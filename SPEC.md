# SPEC — 에브리타임 자동 공감 봇

코드 기준의 동작 사양 문서. 판단 기준이 흔들릴 때 사용할 기준점.
설정값이나 로직 변경 시 SPEC(Specification) 문서도 함께 수정 대상.

## 1. 목적

에브리타임(everytime.kr) 특정 게시판 주기적 스캔, 사용자 취향 프로필에 맞는
게시글에 자동 공감(좋아요) 처리하는 텔레그램 봇.

핵심 원칙: LLM 호출 없이 전부 규칙/통계 기반 알고리즘으로 판단. 게시글의 특성(광고성,
낚시성 등)은 정규식·키워드 매칭·집합 연산으로 `feature_scorer` 모듈이 측정하고,
"공감할지 여부"는 `score_calculator` 모듈이 코드로 결정. 판단 근거를 코드로 추적
가능하게 유지하고 외부 API 의존을 없애기 위한 원칙.

## 2. 모듈 구성

```
app/
  bot.py                    텔레그램 진입점, 핸들러 등록
  config.py                 환경변수/설정 파일 로딩, 상수, 기본값
  core/
    vote_runner.py          공감 실행 오케스트레이션 (run_vote, VoteRunner)
    autonomy.py             자율 실행 스케줄링 (하루 2회 자동 트리거)
    article_trends.py       게시글 시간대 분포 분석 (자율 실행 시간창 추론용)
    database.py             암호화 저장소 (Supabase 기반 key-value)
    clients/
      everytime.py          에브리타임 비공식 API 클라이언트
    services/
      post_filter.py        특성 계산 전 하드 필터 (광고 패턴, 빈 글, 오래된 글)
      feature_scorer.py     규칙/통계 기반 게시글 특성 계산 (외부 API 호출 없음)
      score_calculator.py   특성 점수 -> 최종 점수 -> LIKE(Like Decision)/SKIP(Skip Decision)/REJECT(Reject Decision) 결정
      post_evaluator.py     필터 -> 특성 계산 -> 점수 -> 결정 파이프라인 오케스트레이터
  telegram/
    handlers/                명령어별 핸들러
    messages.py              결과 메시지 포맷팅
```

## 3. 실행 흐름 (`/vote` 1회 또는 자율 실행 1회 기준)

1. `VoteRunner.__init__` — 저장소에서 `etsid`(세션 쿠키), `board_id`(대상 게시판) 로드.
   둘 중 하나라도 없으면 예외 발생, `/setsession` 또는 `/setboard` 안내.
2. `VoteRunner.start()` 순서:
   - `taste_cfg` 로드 (`config/taste.json`)
   - `decision.target_like_rate` 설정 시 적응형 threshold 계산 (5-2절 참조)
   - `FeatureScorer`, `PostEvaluator` 생성
   - `run_vote()` 호출
3. `run_vote()` 순서:
   - `client.check_session()`으로 세션 유효성 확인, 무효 시 즉시 중단
   - 저장된 체크포인트(`last_article_id`) 조회
     - 조회 자체가 저장소 오류로 실패한 경우 "체크포인트 없음"으로 처리하지 않고 실행 중단
       (이미 있는 체크포인트를 무시하고 최신 N페이지만 훑으면 체크포인트 이후 쌓인 글 누락 위험)
   - 체크포인트 유무에 따라 4절 스캔 전략 중 하나로 후보 글 목록(`articles_to_vote`) 수집
   - 후보 글마다: 건너뛸 키워드 검사 -> 이미 공감(`posvote >= 1`)한 글 제외 -> `interest_decider`
     (`PostEvaluator.should_vote`)로 공감 여부 판단 -> `dry_run` 아니면 `push_vote()` 실행
   - 각 글 처리 후 `sleep_min`~`sleep_max` 사이 무작위 대기 (요청 속도 제한 회피용)
   - 실행 종료 후 이번에 스캔한 최신 글로 체크포인트 갱신. 단 `dry_run`이거나 스캔된 글이
     없는 경우는 갱신 제외.

## 4. 스캔 전략과 페이지 상한

페이지 상한 상수 2개를 목적별로 분리. 하나로 합칠 경우 초기 스캔의 안전장치 역할과
체크포인트 탐색의 정확도 요구가 서로 충돌.

| 상수 | 값 | 위치 | 용도 |
|---|---|---|---|
| `bot.max_pages` | 5 (기본값, `config/config.yaml`로 재정의 가능) | `app/config.py` `DEFAULTS` | 최초 실행(체크포인트 없음) 시 훑을 최대 페이지 |
| `CHECKPOINT_SEARCH_MAX_PAGES` | 200 | `app/config.py` | 체크포인트 게시글 탐색 시 훑을 최대 페이지 |

### 4-1. 초기 스캔 (체크포인트 없음, `is_initial = True`)

- 최신 글부터 `bot.max_pages`(기본 5)페이지까지만 스캔 대상
- 스캔한 페이지 수가 `max_pages` 도달 시 `scan_limit_reached = True`
- 목적: 최초 실행 시 게시판 전체 스캔으로 대량 공감이 몰리는 상황 방지용 안전장치.
  최초 실행 전용 제한 — 재개 스캔(4-2절)에는 적용하지 않음.

### 4-2. 재개 스캔 (체크포인트 있음, `is_initial = False`)

1. `client.find_article(board, checkpoint_id, max_pages=...)`로 체크포인트 글 위치 우선
   탐색, 적용 페이지 상한은 체크포인트 탐색 상한(200페이지). 게시판이 비거나(빈 페이지)
   상한 소진까지 탐색 대상 — `bot.max_pages`(5) 제한과 무관.
   - 근거: 봇 재개 시점에 따라 체크포인트 글이 5페이지보다 뒤에 위치 가능. 5페이지로
     제한하면 아직 살아있는 글을 "삭제 추정"으로 오판, 5페이지 이후 쌓인 새 글 전체 누락 위험.
2. 체크포인트 미발견 시(글 삭제 추정) `checkpoint_found = False` 처리, 체크포인트 탐색
   상한까지 재스캔해 스캔된 범위만 후보 처리. `scan_limit_reached`는 실제로 200페이지
   상한 도달 시에만 `True` — 게시판이 자연히 끝나 빈 페이지를 만나 멈춘 경우는 `False`.
3. 체크포인트 발견 시 `checkpoint_found = True`. 최신 글부터 체크포인트 글 직전까지
   후보로 수집(체크포인트 글 자체는 제외 대상). 수집 루프는 페이지 상한 없음 —
   체크포인트 발견 또는 게시판 소진까지 계속.

### 4-3. 체크포인트 갱신 규칙

- 판단/공감 실패 글이 있어도 체크포인트는 이번 실행에서 확인한 최신 글로 갱신.
  실패를 이유로 매번 미룰 경우, 다음 실행에서도 같은 실패가 반복되면 결국 200페이지
  상한 초과로 체크포인트 미발견 상태가 되고, 이후부터는 실패 여부 무관하게
  저장하는 동작이 되므로 처음부터 통일 적용.
- 특성 계산 자체가 예상 밖 예외로 실패한 글(`should_vote` -> `None`)은 `failed` 집계 후
  저장 제외 — 다음 실행에서 재판단 기회 부여.

## 5. 공감 판단 로직

### 5-1. 파이프라인 (`PostEvaluator.should_vote`)

```
캐시 확인 (evaluated_post:{id})
  -> 있으면: 저장된 feature_scores로 현재 taste_cfg 재적용 (재계산 없음)
  -> 없으면: hard_filter() -> feature_scorer -> score_calculator
```

- **하드 필터** (`post_filter.hard_filter`, 특성 계산 전 사전 제외 단계):
  - 제목+본문 공백 또는 2자 미만
  - `skip_keywords`(`/addskip`으로 등록) 포함
  - 전화번호 패턴 포함 (오탐 위험 낮은 명백한 광고 패턴만 대상)
  - `taste.json`의 `hard_filter.max_age_days` 설정 시 해당 일수 초과 글
- **특성 계산** (`FeatureScorer`, `app/core/services/feature_scorer.py`): 외부 API 호출
  없이 정규식/키워드 매칭/집합 연산만으로 특성 8개를 0.0~1.0 값으로 계산. 의미 이해가
  필요해 알고리즘으로 근거 있게 계산할 수 없는 특성(재미, 독창성, 유용성 등)은 제외했다.
  - `topic_relevance`(선호) — `taste.json.topics` 키워드와 제목+본문의 겹침 비율.
    `topics`가 비어 있으면 중립값 0.5
  - `effort`(선호) — 본문 길이 + 문단 구분(줄바꿈 2회 이상) 여부로 계산
  - `information_density`(선호) — 고유 단어 비율 + 숫자 포함 여부
  - `promotion`(감점) — 전화번호 패턴 + URL + 가격 패턴 + `keywords.promotion_terms`
    키워드 매칭 건수 기반
  - `toxicity`(감점) — `keywords.toxicity` 블랙리스트 매칭 건수 기반
  - `clickbait`(감점) — `keywords.clickbait_phrases` 매칭 + 제목의 물음표/느낌표 개수
  - `controversy`(감점) — `keywords.controversy` 블랙리스트 매칭 건수 기반
  - `repetitiveness`(감점) — 같은 게시판 최근 게시글(최대 50개, `recent_fingerprints:{board_id}`
    저장)과의 자카드 유사도 최댓값. 평가할 때마다 이번 글의 토큰을 기록에 추가
  - `confidence` — LLM 자기 보고 대신 본문 길이 기반 함수로 계산 (제목+본문 300자
    이상이면 1.0, 그보다 짧으면 비례해서 낮아짐 — 짧은 글은 규칙 신호의 신뢰도도 낮음)
- **점수 계산** (`score_calculator.calculate_score`):
  - `positive_score` = 선호 특성 가중 평균 (`taste.json.preferences`)
  - `penalty_score` = 감점 특성 가중 평균 (`taste.json.penalties`)
  - `final_score` = `positive_score - penalty_score * penalty_strength`, 0.0~1.0 clamp
- **결정** (`score_calculator.make_decision`):
  1. `hard_reject` 항목 중 하나라도 `taste.json.hard_reject` 임계값 이상이면 즉시 REJECT
  2. `confidence < min_confidence`면 SKIP (본문이 짧아 규칙 신호를 못 미더워하는 케이스)
  3. `final_score >= threshold`면 LIKE
  4. `threshold - exploration <= final_score < threshold`이고 `penalty_score < 0.4`인 경우,
     30% 확률로 LIKE (탐색 목적 — 취향 프로필 과도한 고정 방지용)
  5. 위 조건 전부 미해당 시 SKIP

### 5-2. threshold 결정 방식 2가지

`taste.json.decision.target_like_rate` 값으로 전환.

- **고정 threshold** (`target_like_rate = null`): `decision.threshold` 값 사용, `strictness`
  만큼 `effective_threshold()`로 최대 0.15까지 상향 조정.
- **적응형 threshold** (`target_like_rate`에 0.0~1.0 값 설정): 최근 평가된 글(최대 300개,
  hard_filter 제외 글은 미포함) 중 상위 N% 지점 점수를 매 실행 threshold로 재계산.
  표본 30개 미만 시 통계적 신뢰 부족으로 판단, `decision.threshold`를
  fallback으로 사용. 게시판 콘텐츠 점수 분포가 시간 경과에 따라 전체적으로 오르내려도
  목표 비율 유지 가능, threshold 수동 재조정 필요성 감소. 적응형 방식에서 `strictness`는
  절대 점수가 아니라 목표 비율 자체를 줄이는 용도로 사용.

## 6. 설정 파일

### 6-1. `app/config/config.yaml` (선택 파일, 미존재 시 기본값 적용)

| 키 | 기본값 | 설명 |
|---|---|---|
| `bot.max_pages` | 5 | 초기 스캔 최대 페이지 (4-1절 참조) |
| `timing.sleep_min` / `sleep_max` | 1.0 / 3.0 | 공감 요청 사이 무작위 대기(초) |
| `timing.page_delay` | 0.5 | 페이지 조회 사이 대기(초) |

### 6-2. `app/config/taste.json` (취향 프로필, 미존재 시 내장 기본값 적용)

- `preferences` — 선호 특성별 가중치 (topic_relevance, effort, information_density)
- `penalties` — 감점 특성별 가중치 (promotion, toxicity, clickbait, controversy,
  repetitiveness)
- `decision` — `threshold`, `strictness`, `exploration`, `penalty_strength`,
  `min_confidence`, `target_like_rate`
- `hard_reject` — 특성별 즉시 REJECT 임계값 (선택 항목)
- `topics` — 관심 주제 목록. `topic_relevance` 측정 시 참고 대상
- `keywords` — 규칙 기반 특성 계산용 키워드 블랙리스트 (`toxicity`, `controversy`,
  `clickbait_phrases`, `promotion_terms`). 소규모 스타터 세트이므로 직접 추가/편집 전제
- `hard_filter.max_age_days` — 해당 일수 초과 글은 특성 계산 없이 제외 (선택 항목)

값 검증(`_validate_taste_config`) 처리 내용: 알 수 없는 키 무시, 범위 이탈 값은 기본값으로
대체 — 설정 파일 손상 시에도 봇 정상 동작 유지 목적.

## 7. 저장소 (Supabase `bot_storage` 테이블)

키-값 구조, `value`는 Fernet 암호화 저장 대상. 주요 키:

| 키 | 내용 |
|---|---|
| `etsid` | 에브리타임 세션 쿠키 |
| `board_id` / `board_name` | 선택된 게시판 |
| `last_article_id` | 체크포인트 (마지막 확인 최신 글 ID) |
| `skip_keywords` | 건너뛸 키워드 목록 (JSON 배열) |
| `run_history` | 최근 실행 기록 (최대 30개, JSON 배열) |
| `evaluated_post:{id}` | 게시글별 평가 결과 캐시 (특성 점수, 최종 점수, 결정, 공감 성사 여부) |
| `recent_fingerprints:{board_id}` | repetitiveness 계산용 게시판별 최근 게시글 토큰 목록 (최대 50개) |
| `autonomy_slot:{date}:{slot}` | 자율 실행 슬롯 중복 실행 방지용 선점 키 |
| `last_run_time` | 마지막 실행 시각 |

## 8. 텔레그램 명령어

인가된 채팅 ID(환경변수로 설정, 10절 참조)만 응답 대상 (`_authorized` 검사).

| 명령어 | 기능 |
|---|---|
| `/start` | 공감 실행 여부 확인 후 안내 |
| `/setsession` | etsid 쿠키 값 저장 |
| `/setboard` | 공감 대상 게시판 선택 (인라인 키보드) |
| `/vote` | 공감 실행 (동시 실행 방지 락 적용) |
| `/menu` | 공감 실행 버튼 표시 |
| `/addskip`, `/removeskip`, `/listskip` | 건너뛸 키워드 관리 |
| `/stats` | 실행 통계 (오늘/이번주/전체, 추세, 스캔 유형별, 시간대별) |
| `/togglestat <번호>` | 통계 레코드 유효/제외 토글 |
| `/deletestat <번호>` | 통계 레코드 영구 삭제 |
| `/status` | 세션/게시판/마지막 실행 시각 확인 |
| `/profile` | 현재 취향 프로필(가중치, threshold 방식) 확인 |
| `/help` | 명령어 목록 |

## 9. 자율 실행 (autonomy)

- 하루 2개 슬롯(A: 오전, B: 오후)에 `/vote`와 동등한 동작 자동 실행 대상.
- 기본 시간창: A `06:00~09:00`, B `17:00~20:00`. 매일 자정 5분(`00:05 KST`)에
  최근 7일 게시글 시간대 분포 분석(`article_trends.infer_activity_windows`)해 다음
  날 시간창 갱신 대상. 표본 부족 시 기본 시간창으로 대체.
- 슬롯별 실행 시각은 시간창 내 무작위 결정 (규칙적 매크로 패턴 회피 목적).
- 실행 직전 "활동 감지" 검사 수행: 메시지함 안읽음 수를 일정 간격 두고 2회 조회,
  변화 없으면(사용자 비활동 상태로 판단) 실행 진행. 최대 3회 재시도 후에도 활동
  감지 시 해당 슬롯 건너뛰기 처리.
- 슬롯당 1회 실행만 보장하도록 `db.claim_once()`로 원자적 선점 처리 (중복 실행 방지용).
- 수동 `/vote` 진행 중이면 자동 슬롯 건너뛰기 대상 (락 공유).
- 실행 직전 본인 작성 글(`is_mine=True`, 공감 미기록 글) 삭제 후 공감 실행 순서.

## 10. 환경변수

| 변수 | 용도 |
|---|---|
| `TELEGRAM_TOKEN` | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 인가된 채팅 ID (숫자) |
| `SUPABASE_URL` / `SUPABASE_KEY` | 저장소 접속 정보 |
| `ENCRYPTION_KEY` | 저장소 값 암호화 키 (미설정 시 최초 실행에서 자동 생성) |
| `DRY_RUN` | `1`/`true`/`yes` 설정 시 실제 공감 요청 없이 판단만 수행 |

## 11. 테스트

`python -m pytest` 로 전체 테스트 실행 대상. 주요 테스트 파일: `test/test_voter.py`
(공감 실행 로직), `test/test_autonomy.py`(자율 실행 스케줄링), `test/test_feature_scorer.py`
(규칙 기반 특성 계산), `test/test_config.py`(taste.json 검증). 로직 변경 시 관련 테스트
우선 확인, 기존 테스트가 새 요구사항과 불일치하면 테스트부터 수정 후 구현 반영.
