# korvid — AI-Native Kubernetes TUI 설계 문서

- **작성일**: 2026-07-23
- **상태**: Draft (사용자 리뷰 대기)
- **작업명**: `korvid` (corvid=까마귀과, 도구를 사용하는 조류 — 네이밍 리서치로 GitHub·PyPI 무충돌 확인, 2026-07-23 확정)

---

## 1. 비전

**"k9s의 키보드 퍼스트 클러스터 조작 경험"과 "Claude Code급 대화형 AI 에이전트"가 한 화면에서 공존하는 Kubernetes 진단/운영 콕핏.**

- 평상시: k9s처럼 빠른 키보드 퍼스트 리소스 탐색·조작 TUI
- 에이전트 활성화 시: 에이전트가 클러스터를 조사하고, **TUI 화면을 직접 운전**(뷰 전환·필터·로그 열기)하면서 근거를 보여주며 진단. 명령 실행은 승인 게이트를 거침
- 어느 순간에도 사용자는 키보드로 개입 가능 — TUI 경험을 잃지 않음

### 왜 지금인가 (리서치 근거, 2026-07 기준)
- "풀스크린 TUI + 대화형 LLM + 명령 실행"을 모두 갖춘 성숙 도구 부재 (유일 시도 ks-ai는 7★ PoC 방치)
- k9s는 AI 통합 계획 없음 (네이티브 통합 PR #3803, #3426 모두 stale 폐기), 1인 메인테이너 재정난
- Python/Textual 스택 실증 완료 (posting 12k★, elia 2.5k★, parllama 활동 중)
- 최대 위협은 범용 에이전트(Claude Code + K8s MCP) → **화면 컨텍스트 자동 주입 + TUI 운전 + 승인·감사 체계**라는 도메인 특화로 차별화

---

## 2. 목표 사용자 / 핵심 시나리오

**타깃**: 터미널에서 K8s를 운영하는 SRE/플랫폼 엔지니어/백엔드 개발자 (k9s 사용자층과 동일)

핵심 시나리오:
1. **탐색**: `:pods`, `/filter`, 로그 tail — k9s와 동등하거나 나은 속도
2. **대화형 진단**: 에이전트 패널 열고 "checkout 서비스 왜 5xx 나?" → 에이전트가 events/logs/스펙을 조사하며 관련 뷰를 화면에 띄워 근거 제시 → 원인+수정안 제안
3. **승인 기반 수정**: 에이전트가 `kubectl rollout restart ...` 제안 → diff/명령 미리보기 → 사용자 승인 → 실행 → 감사 로그 기록
4. **화면 컨텍스트 질문**: CrashLoopBackOff pod에 커서 두고 단축키 → "이 pod 왜 죽어?"가 컨텍스트(리소스 전체 spec, 최근 events, 로그 tail) 자동 첨부로 질의됨

---

## 3. 검토한 접근 방식

| | A. 사이드카 채팅 | B. 에이전트-퍼스트 셸 | **C. 듀얼 모드 하이브리드 (채택)** |
|---|---|---|---|
| 개요 | TUI + 읽기전용 Q&A 패널 | 대화가 주 UI, 위젯은 결과물 | 키보드 퍼스트 TUI + 에이전트가 TUI를 운전 가능 |
| 장점 | 단순, 저리스크 | AI 차별화 최대 | 두 요구 모두 충족, 구조적 차별화 |
| 단점 | 요구 미충족, kubectl-ai와 차이 없음 | TUI 경험 상실, 범용 에이전트와 정면 경쟁 | 복잡도 최고 |
| 판정 | ❌ | ❌ | ✅ 단계적 로드맵으로 복잡도 관리 |

---

## 4. 아키텍처

### 4.1 전체 구조 (단일 프로세스, asyncio)

```
┌─────────────────────────────────────────────────────────────┐
│                     Textual App (asyncio)                   │
│  ┌────────────────────────────┐  ┌───────────────────────┐  │
│  │        UI Layer            │  │    Agent Panel        │  │
│  │  WorkspaceScreen           │  │  chat / tool-call log │  │
│  │  ├─ Pane(ResourceTable)    │  │  approval dialogs     │  │
│  │  ├─ Pane(LogView)          │  │  streaming markdown   │  │
│  │  └─ StatusBar/CmdPalette   │  └──────────┬────────────┘  │
│  └──────────┬─────────────────┘             │               │
│             │ UI Bus (commands/events)      │               │
│  ┌──────────┴─────────────────┐  ┌──────────┴────────────┐  │
│  │      Core Services         │  │    Agent Runtime      │  │
│  │  WatchManager (selective)  │  │  agentic loop         │  │
│  │  ResourceStore (cache)     │  │  ToolRegistry         │  │
│  │  LogStreamer (multi-pod)   │  │  ├─ k8s read tools    │  │
│  │  ActionExecutor            │  │  ├─ k8s write tools ⚠ │  │
│  │  ContextManager (kubecfg)  │  │  ├─ ui control tools  │  │
│  │  AuditLog                  │  │  └─ shell tool ⚠      │  │
│  └──────────┬─────────────────┘  │  LLM ProviderAdapter  │  │
│             │                    └──────────┬────────────┘  │
│  ┌──────────┴────────────────────────────── ┴────────────┐  │
│  │  kubernetes.aio (async client, watch streams)         │  │
│  │  LLM APIs (OpenAI/Anthropic/Gemini/Azure/Ollama)      │  │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
⚠ = 승인 게이트 필수
```

**핵심 설계 원칙 — UI Bus**: 사용자의 키 입력도, 에이전트의 UI 제어 도구도 **동일한 커맨드 버스**를 통과한다 (`navigate(view=pods, ns=prod)`, `set_filter(...)`, `open_logs(...)`). 이 단일 진입점이:
- 에이전트의 "다이렉트 TUI 핸들링"을 자연스럽게 구현 (사람이 하는 것과 같은 액션을 발행)
- 모든 상태 변화를 감사 로그에 일원화
- 테스트 가능성 확보 (버스에 커맨드 주입 → 상태 검증)

### 4.2 기술 스택

| 구성요소 | 선택 | 근거 |
|---|---|---|
| 언어 | Python ≥3.11 | asyncio TaskGroup, 성숙한 생태계 |
| TUI | Textual (v8+) | 36.7k★, 활발 유지, 컴포저블 레이아웃(분할 패널 무료), async 네이티브 |
| K8s 클라이언트 | kubernetes-client/python v36+ (`kubernetes.aio`) | 공식, async watch 스트림, Textual 이벤트 루프와 자연 결합 |
| LLM 어댑터 | 자체 얇은 어댑터 (OpenAI-호환 + Anthropic + Gemini + Ollama) | litellm은 무겁고 의존성 리스크. tool-use 루프는 자체 소유 필요 |
| 패키징 | uv + `pipx/uvx` 설치, 추후 단일 바이너리(PyApp/PEX) 검토 | k9s의 "단일 바이너리" 강점 추격 |
| 설정 | **단일 `~/.config/korvid/config.yaml`** | k9s 페인 #7 (7개 파일/3개 디렉토리) 원천 회피 |

---

## 5. k9s 페인 포인트 → 설계 반영 (Day-1 요구사항)

리서치로 확인된 상위 페인 포인트(이슈 번호·👍 수 실측)와 대응:

| # | k9s 페인 포인트 (근거) | korvid 설계 대응 | 단계 |
|---|---|---|---|
| 1 | 로그 뷰어 부실 — 불완전 로그(#1228, 83👍), EOF(#1399, 4.5년 open), 멀티-pod 불가(#827, 62👍), JSON 파싱 7년 미구현(#364, 147👍) | **로그를 1급 시민으로**: 멀티-pod 병합 스트림(pod 프리픽스), JSON 자동 감지+필드 추출, 버퍼 초과 명시 배너, 재연결 상태 표시, 검색 히트 수+n/N 내비게이션 | MVP |
| 2 | 동시 다중 뷰 불가 (#351 7년, #1430 40👍 NOT PLANNED) — tview PageStack 구조적 한계 | **분할 패널 아키텍처 Day-1**: WorkspaceScreen이 N개 Pane 관리 (로그 보면서 pod 목록 감시). Textual Container로 구현 | MVP (2-pane) |
| 3 | 키바인딩 오버라이드 불가 (#625, 6년 "noodle") | 모든 액션이 커맨드 버스의 named command → `keybindings:` 섹션에서 전부 재매핑 가능 | MVP |
| 4 | 위험 조작 안전장치 부재 — 네임스페이스 오삭제(#1016), Ctrl-K 즉시 kill(#319) | **계층형 확인**: 일반 삭제=다이얼로그, 클러스터 스코프 삭제=리소스명 타이핑 확인, protected context=추가 확인+빨간 헤더, `--readonly` 모드 | MVP |
| 5 | RBAC/인증 에러 불투명 (#3730 — 401이 "command not found"로 표시) | API 에러 명시 파싱: "pods/exec 권한 없음(ns: prod)", 토큰 만료 감지→재인증 안내, 권한 없는 액션은 dim 처리 | MVP |
| 6 | API 서버 해머링 (#3603, 28👍, "as-designed"로 방치) | **선택적 watch**: 화면에 보이는 리소스만 watch, 비포커스 시 일시정지, 지수 백오프, watch 북마크 활용 | MVP |
| 7 | 설정 파일 7개/3개 디렉토리 스프롤 | 단일 config.yaml + `contexts.<name>:` 오버라이드 섹션, 무설정 실행 가능(kubeconfig 자동 감지) | MVP |
| 8 | 플러그인 = shell-out only, UI 확장 불가 (#771, 160👍) | Python 플러그인 API: 커스텀 패널/컬럼/커맨드/에이전트 도구 등록. 스키마 검증 manifest | Phase 3 |
| 9 | Secret base64 수작업 (#1017, 42👍, 4년+ open) | Secret 뷰/편집 시 자동 디코드↔인코드 라운드트립 기본 | Phase 2 |
| 10 | 업그레이드 시 크래시/panic (#2465, 69👍) — UI·데이터 고루틴 미분리 | UI/데이터 태스크 분리 + 전역 예외 경계: 데이터 계층 예외는 패널 내 에러 카드로 격리, 앱은 생존 | MVP |
| 11 | 메트릭 정렬 회귀 반복 (#3793, 24👍, 6개월 미수정) | 데이터 모델·렌더링 분리 + 정렬/메트릭 단위 테스트 커버리지 | Phase 2 |
| 12 | 1인 메인테이너 병목 (stale bot이 완성 PR도 폐기) | 코어 최소화 + 플러그인으로 확장 위임, CI/테스트 자동화로 기여 장벽 완화 | 운영 방침 |
| 13 | 키바인딩 발견성 부족 | 컨텍스트 인식 도움말 + fuzzy 커맨드 팔레트(액션 이름으로 검색→키 표시) + **에이전트에게 물어보기** | Phase 2 |
| 14 | 터미널 호환성 (#3598 Windows Terminal 대비 불량) | Textual의 테마/컬러 시스템 활용 (자체 컬러 관리보다 안전) | 무료 획득 |
| 15 | Helm 조작 얕음 (#1841, 28👍) | 비목표(MVP) — Phase 3+에서 플러그인으로 | Phase 3+ |

**보존할 k9s 강점**: `:` 커맨드 바 + vim 스타일 내비게이션, 즉시 ctx/ns 전환, 빠른 기동, `/` 필터(regex/fuzzy/label), 원키 shell-in, 포트포워드 관리, read-only 모드.

---

## 6. 에이전트 설계 (차별화 핵심)

### 6.1 UX — "Claude Code 느낌, k9s 경험 유지"

- `Ctrl-A`(가칭)로 에이전트 패널 토글 — 우측 30~40% 패널로 슬라이드 인. 나머지 화면은 여전히 살아있는 TUI
- 패널 구성: 스트리밍 마크다운 응답 + **tool-call 로그**(Claude Code처럼 접을 수 있는 "🔧 get_pod_logs(checkout-7d9f…) ✓") + 입력창
- **컨텍스트 자동 주입**: 현재 화면 상태(활성 뷰, 선택 리소스, 적용 필터, ns/ctx)가 항상 시스템 컨텍스트로 제공. 선택 리소스에서 단축키로 "이거 왜 이래?" 즉시 질의
- **에이전트 드라이브 모드**: 에이전트가 UI 제어 도구로 화면을 조작할 때 해당 패널에 시각적 표시(테두리 하이라이트 + "agent" 배지). 사용자 키 입력이 들어오면 즉시 사용자 우선

### 6.2 에이전틱 루프 & 도구

자체 tool-use 루프 (max iterations 기본 15, 설정 가능):

| 도구 그룹 | 예시 | 게이트 |
|---|---|---|
| k8s read | `list_resources`, `get_resource`, `get_logs`, `get_events`, `top_pods`, `explain_rbac` | 없음 (RBAC 준수) |
| k8s write | `apply`, `delete`, `scale`, `rollout_restart`, `cordon` | **승인 필수** — 명령/diff 미리보기 다이얼로그 |
| UI control | `navigate`, `set_filter`, `open_logs`, `split_pane`, `highlight_resource` | 없음 (화면만 변경, 시각 표시) |
| shell | `run_kubectl(args)` (화이트리스트 검증) | read 동사는 통과, write 동사는 승인 |

- **읽기전용 기본** 철학 (HolmesGPT 방식) + **승인 기반 실행** (kubectl-ai 방식) 결합
- 승인 다이얼로그: 실행할 정확한 명령 + 대상 리소스 + dry-run diff(가능 시) 표시, Y/n/edit
- **감사 로그**: 모든 write 실행(사용자·에이전트 불문)을 `~/.local/state/korvid/audit.jsonl`에 기록 (who/when/command/approved-by)
- 프라이버시: LLM 전송 데이터 anonymize 옵션 (k8sgpt 방식), Secret 값은 기본 마스킹

### 6.3 LLM 프로바이더

- 어댑터 인터페이스: `complete(messages, tools, stream=True)` — OpenAI-호환(OpenAI/Azure/Ollama/vLLM), Anthropic, Gemini 어댑터 제공
- 모델/키는 config 또는 env. 프로바이더 미설정 시 에이전트 기능만 비활성화되고 TUI는 완전 동작 (**LLM 없이도 쓸모 있는 도구**여야 함)

---

## 7. 데이터 계층

- **WatchManager**: 뷰가 구독하는 (group, version, kind, ns) 단위로 watch 태스크 생성/공유/해제. 화면에 없는 리소스는 watch하지 않음(페인 #6). 재연결은 지수 백오프 + resourceVersion 북마크
- **ResourceStore**: watch 이벤트를 반영하는 인메모리 캐시. UI는 스토어의 reactive 스냅샷만 구독 (데이터↔렌더링 분리, 페인 #10/#11)
- **LogStreamer**: pod당 스트림 태스크, 멀티-pod 병합 큐, 링버퍼(기본 5만 라인, 초과 시 명시 배너), 재연결 상태 이벤트
- **에러 격리**: 데이터 태스크 예외는 구조화 에러 이벤트로 변환 → 해당 패널에 에러 카드 렌더. 앱 크래시 금지

## 8. 에러 처리 원칙

1. K8s API 에러는 status code + reason 파싱 → 사용자 언어로 변환 (401→"인증 만료", 403→"권한 부족: {verb} {resource}")
2. 에이전트 도구 실패는 루프에 에러 결과로 반환 (에이전트가 우회 시도 가능)
3. LLM API 장애는 패널 내 표시, TUI 본체 무영향
4. 전역 예외 경계: 최후의 예외도 크래시 리포트 파일 저장 후 우아한 종료

## 9. 테스트 전략

- **Core Services**: pytest + pytest-asyncio, fake K8s API(respx/녹화 픽스처)로 WatchManager/LogStreamer/에러 매핑 단위 테스트
- **UI**: Textual의 `Pilot` 테스트 러너로 키 입력→화면 상태 검증 (커맨드 버스 덕에 스냅샷 테스트 용이)
- **Agent**: LLM 모킹(고정 tool-call 시퀀스)으로 루프/승인 게이트/감사 로그 검증. 승인 게이트 우회 불가를 보장하는 테스트 필수
- **E2E(후순위)**: kind 클러스터 대상 스모크 테스트

## 10. 로드맵

| 단계 | 범위 | 완료 기준 |
|---|---|---|
| **Phase 1 — MVP** | pods/deploy/svc/events/ns/ctx 뷰, `:` 팔레트, `/` 필터, 2-pane 분할, 1급 로그 뷰어(멀티-pod/JSON/재연결), 계층형 안전장치, RBAC 에러 매핑, 선택적 watch, 단일 config, 키바인딩 오버라이드, **에이전트 패널(read 도구 + UI 제어 + 승인 기반 kubectl write)**, 감사 로그 | 일상 진단 워크플로를 k9s 없이 수행 가능 |
| **Phase 2** | 전체 리소스+CRD 자동 감지, shell-in, 포트포워드, Secret 디코드 편집, 메트릭(top) 정렬, 커맨드 팔레트 발견성, 세션 상태 복원, anonymize | k9s 일상 사용 대체 가능 |
| **Phase 3** | Python 플러그인 API(패널/컬럼/에이전트 도구 등록), 외부 MCP 서버 연결(에이전트 도구 확장), 멀티 클러스터 동시 뷰, 진단 플레이북 | 생태계 확장 개시 |
| **비목표** | 웹 UI, 클러스터 내 상주 에이전트(kagent 영역), Helm 관리(초기), 1000+노드 초대형 클러스터 최적화 | — |

## 11. 리스크 & 완화

| 리스크 | 완화 |
|---|---|
| 범용 에이전트(Claude Code+MCP)가 니치 잠식 | 화면 컨텍스트 주입·TUI 운전·승인/감사 체계는 범용 도구가 못 하는 것. Phase 3에서 역으로 MCP 클라이언트가 되어 생태계 흡수 |
| Python 성능 (대규모 watch) | 선택적 watch + async 아키텍처. 초대형 클러스터는 명시적 비목표 |
| Textual 버스 팩터=1 | MIT, 대형 커뮤니티, 최악 시 포크. 추상화 계층으로 UI 프레임워크 결합도 관리 |
| kubectl-ai가 TUI 추가 | 선점 속도. Google은 REPL/웹에 집중 중 |
| 에이전트 오동작으로 클러스터 손상 | write는 승인 게이트 기본 강제. `--skip-approvals` 옵션으로 완화 가능하되 **protected context에서는 옵션 무시하고 무조건 승인 요구**. 감사 로그, read-only 모드 |

## 12. 미확정 사항 (사용자 결정 필요)

1. ~~**제품명**~~ → **`korvid` 확정** (2026-07-23, 네이밍 리서치: GitHub K8s 니치·PyPI 무충돌 확인. corvid=까마귀과, 도구를 사용하는 조류 → agentic tool-use 은유)
2. 기본 LLM 프로바이더 권장값 (사내 표준 유무)
3. 대상 배포 채널 (PyPI만? Homebrew? 사내 전용?)
4. 라이선스 (OSS 공개 여부)
