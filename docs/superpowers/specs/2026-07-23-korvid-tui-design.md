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
- "풀스크린 TUI + 대화형 LLM + 명령 실행"을 모두 갖춘 성숙 도구가 아직 없음 (유일 시도 ks-ai는 7★ PoC 초기 단계에 머묾)
- 기존 TUI 생태계에 AI 통합 로드맵이 없음 (k9s 네이티브 통합 PR #3803, #3426은 머지에 이르지 못함) — 이 조합은 새 설계로 시작할 때 자연스럽게 구현 가능한 영역
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
| 설정 | **단일 `~/.config/korvid/config.yaml`** | 설정 파일 스프롤 없이 처음부터 단일 파일 (§5 #7) |

---

## 5. 기존 도구에서 채워지지 않던 요구 → 설계 반영 (Day-1 요구사항)

터미널 K8s 워크플로에서 커뮤니티가 오랫동안 원했지만 아직 채워지지 않은 요구들을 실측(이슈 번호·👍 수)으로 확인했다. k9s 이슈 트래커는 이 수요를 보여주는 가장 좋은 데이터 소스다 — k9s가 증명해 준 수요의 지도이며, korvid는 새 설계이기에 이 요구들을 처음부터 반영할 수 있다:

| # | 미충족 요구 (수요 근거) | korvid 설계 반영 | 단계 |
|---|---|---|---|
| 1 | 로그 워크플로 고도화 — 멀티-pod 통합 뷰(#827, 62👍), JSON 로그 파싱(#364, 147👍), 안정적 스트리밍(#1228 83👍, #1399) | **로그를 1급 시민으로**: 멀티-pod 병합 스트림(pod 프리픽스), JSON 자동 감지+필드 추출, 버퍼 초과 명시 배너, 재연결 상태 표시, 검색 히트 수+n/N 내비게이션 | MVP |
| 2 | 동시 다중 뷰 (#351, #1430 40👍) — 기존 단일-뷰 스택 구조에서는 구현이 어려웠던 기능 | **분할 패널 아키텍처 Day-1**: WorkspaceScreen이 N개 Pane 관리 (로그 보면서 pod 목록 감시). Textual Container로 구현 | MVP (2-pane) |
| 3 | 키바인딩 자유 오버라이드 (#625) | 모든 액션이 커맨드 버스의 named command → `keybindings:` 섹션에서 전부 재매핑 가능 | MVP |
| 4 | 위험 조작에 대한 더 강한 안전장치 (#1016, #319) | **계층형 확인**: 일반 삭제=다이얼로그, 클러스터 스코프 삭제=리소스명 타이핑 확인, protected context=추가 확인+빨간 헤더, `--readonly` 모드 | MVP |
| 5 | RBAC/인증 에러의 명확한 표면화 (#3730) | API 에러 명시 파싱: "pods/exec 권한 없음(ns: prod)", 토큰 만료 감지→재인증 안내, 권한 없는 액션은 dim 처리 | MVP |
| 6 | API 서버 부하 최소화 (#3603, 28👍) | **선택적 watch**: 화면에 보이는 리소스만 watch, 비포커스 시 일시정지, 지수 백오프, watch 북마크 활용 | MVP |
| 7 | 단순한 설정 체계 | 단일 config.yaml + `contexts.<name>:` 오버라이드 섹션, 무설정 실행 가능(kubeconfig 자동 감지) | MVP |
| 8 | UI까지 확장 가능한 플러그인 (#771, 160👍 — 기존 플러그인 모델은 shell-out 방식) | Python 플러그인 API: 커스텀 패널/컬럼/커맨드/에이전트 도구 등록. 스키마 검증 manifest | Phase 3 |
| 9 | Secret base64 자동 디코드 (#1017, 42👍) | Secret 뷰/편집 시 자동 디코드↔인코드 라운드트립 기본 | Phase 2 |
| 10 | 런타임 안정성 (#2465, 69👍) — UI·데이터 계층 격리의 중요성 시사 | UI/데이터 태스크 분리 + 전역 예외 경계: 데이터 계층 예외는 패널 내 에러 카드로 격리, 앱은 생존 | MVP |
| 11 | 메트릭 정렬 신뢰성 (#3793, 24👍) | 데이터 모델·렌더링 분리 + 정렬/메트릭 단위 테스트 커버리지 | Phase 2 |
| 12 | 지속 가능한 유지보수 구조 — 소수 메인테이너 프로젝트의 공통 과제 | 코어 최소화 + 플러그인으로 확장 위임, CI/테스트 자동화로 기여 장벽 완화 | 운영 방침 |
| 13 | 키바인딩 발견성 | 컨텍스트 인식 도움말 + fuzzy 커맨드 팔레트(액션 이름으로 검색→키 표시) + **에이전트에게 물어보기** | Phase 2 |
| 14 | 폭넓은 터미널 호환성 (#3598) | Textual의 테마/컬러 시스템 활용 (자체 컬러 관리보다 안전) | 무료 획득 |
| 15 | 더 깊은 Helm 워크플로 (#1841, 28👍) | 비목표(MVP) — Phase 3+에서 플러그인으로 | Phase 3+ |

**계승할 k9s의 검증된 UX**: `:` 커맨드 바 + vim 스타일 내비게이션, 즉시 ctx/ns 전환, 빠른 기동, `/` 필터(regex/fuzzy/label), 원키 shell-in, 포트포워드 관리, read-only 모드. 사용자 전환 비용을 최소화하기 위해 이 관습을 그대로 따른다.

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
| debug | `launch_debug_session` (이미지·타깃·프로파일 구성), `suggest_debug_commands` | **승인 필수** (pod spec 변경) |
| UI control | `navigate`, `set_filter`, `open_logs`, `split_pane`, `highlight_resource` | 없음 (화면만 변경, 시각 표시) |
| shell | `run_kubectl(args)` (화이트리스트 검증) | read 동사는 통과, write 동사는 승인 |

- **읽기전용 기본** 철학 (HolmesGPT 방식) + **승인 기반 실행** (kubectl-ai 방식) 결합
- 승인 다이얼로그: 실행할 정확한 명령 + 대상 리소스 + dry-run diff(가능 시) 표시, Y/n/edit
- **감사 로그**: 모든 write 실행(사용자·에이전트 불문)을 `~/.local/state/korvid/audit.jsonl`에 기록 (who/when/command/approved-by)
- 프라이버시: LLM 전송 데이터 anonymize 옵션 (k8sgpt 방식), Secret 값은 기본 마스킹

### 6.3 LLM 프로바이더 & 활성화 모델

- 어댑터 인터페이스: `complete(messages, tools, stream=True)` — OpenAI-호환(OpenAI/Azure/Ollama/vLLM), Anthropic, Gemini 어댑터 제공
- 모델/키는 config 또는 env. 프로바이더 미설정 시 에이전트 기능만 비활성화되고 TUI는 완전 동작 (**LLM 없이도 쓸모 있는 도구**여야 함)

**활성화 모델 — "provider를 제공하면 합쳐진다" (설정 감지 자동 활성화 + 명시적 안전장치)**:

1. **단일 패키지**: 에이전트 런타임은 항상 포함해 배포한다 (어댑터는 자체 얇은 HTTP 구현이라 별도 extra 분리의 이점이 적음)
2. **자동 merge**: config/env에서 provider 설정이 감지되면 에이전트가 활성화된다 — `Ctrl-A` 패널이 살아나고 상태바에 모델명 표시. 설정 파일 하나 추가하면 끝, 별도 설치·플래그 불필요
3. **미설정 시 안내**: provider가 없을 때 `Ctrl-A`를 누르면 "provider를 설정하면 에이전트가 활성화됩니다" + config 예시를 패널에 표시 (기능의 존재를 발견 가능하게)
4. **명시적 오프 스위치**: provider가 있어도 `agent.enabled: false`로 전역 비활성화 가능. 컨텍스트별 오버라이드(`contexts.<name>.agent.enabled`)로 특정 클러스터에서만 끌 수 있음
5. **protected context 연동**: protected context로 지정된 클러스터에서는 에이전트 write 도구가 승인 게이트를 절대 우회할 수 없고(§6.2), 필요 시 에이전트 자체를 자동 비활성화하는 옵션(`agent.disable_in_protected: true`) 제공 — 규제 환경에서 "실수로 클러스터 데이터를 LLM에 전송"을 구조적으로 차단

### 6.4 라이브 디버깅 — `kubectl debug` 통합 (기존 TUI에 없던 기능)

운영 중인 pod에 대한 무중단 디버깅을 1급 기능으로 제공한다. kubectl debug의 세 모드를 모두 커버하되 단계적으로 도입한다.

| 모드 | 용도 | UX | 단계 |
|---|---|---|---|
| **Ephemeral container** | 운영 pod 무중단 디버깅. distroless/최소 이미지라 셸이 없는 pod에 필수 | pod 뷰에서 `d` → 디버그 다이얼로그 → attach | **MVP** |
| Copy-of-pod (`--copy-to`) | 원본 불가침 실험 (명령/이미지 교체) | 다이얼로그에서 모드 선택. 세션 종료 시 사본 pod 정리 여부 확인 | Phase 2 |
| Node debug | 노드 레벨 진단 (호스트 네임스페이스) | node 뷰에서 `d` | Phase 2 |

**디버그 다이얼로그** — kubectl debug의 복잡한 플래그 조합을 폼 UI로 해결:
- 디버그 이미지: 프리셋(busybox, nicolaka/netshoot, ubuntu) + 사용자 정의(config의 `debug.images`에 사내 레지스트리 이미지 등록 가능)
- `--target` 컨테이너 선택 (프로세스 네임스페이스 공유 대상)
- 프로파일: `general`(기본) / `netadmin` / `sysadmin` / `restricted` — 각 프로파일의 권한 차이를 다이얼로그에 설명 표기

**터미널 attach**: MVP는 TUI suspend → PTY로 `kubectl debug -it` 실행 → 종료 시 TUI 복귀 (k9s shell-in과 동일 패턴, 검증된 방식). Phase 3에서 분할 패널 내 임베디드 터미널 검토.

**안전 설계**:
- Ephemeral container 주입은 pod spec 변경(write)이므로 **승인 다이얼로그 + 감사 로그** 경유
- ⚠️ **주입된 ephemeral container는 pod 재시작 전까지 제거 불가** — 이 caveat를 승인 다이얼로그에 명시하고, 활성 디버그 컨테이너가 있는 pod는 목록에 배지 표시
- RBAC 사전 검증: `pods/ephemeralcontainers` update 권한 없으면 "권한 부족: pods/ephemeralcontainers" 명시 (§5 #5 원칙)
- 클러스터 버전 검증: EphemeralContainers는 K8s 1.25+ stable — 미만 버전에서는 기능 비활성화 + 사유 표시

**🌟 에이전트 통합 (차별화 킬러 워크플로)**:
- `launch_debug_session` 도구: 에이전트가 진단 맥락에 맞는 디버그 구성을 스스로 결정해 제안 — "이 pod 네트워크가 왜 안 돼?" → netshoot 이미지 + `--target app` + netadmin 프로파일 구성 → 사용자 승인 → 셸 진입
- `suggest_debug_commands` 도구: 진입 후 실행할 진단 명령 시퀀스 제안 (예: `nslookup svc`, `ss -tlnp`, `tcpdump -i eth0`)
- 시나리오 완결성: "증상 질문 → 에이전트 조사 → 디버그 세션 자동 구성 → 셸에서 검증"이 한 흐름으로 이어짐. 기존에는 대화형 진단(REPL 도구)과 수동 shell-in(TUI)이 별개 도구로 나뉘어 있던 흐름을 하나로 잇는 지점

### 6.5 확장 진단 기능 — 바닐라 K8s API만 사용 (2026-07-23 리서치)

외부 생태계 도구(Helm/GitOps/보안 스캐너/비용/멀티클러스터) 없이 **바닐라 Kubernetes API + metrics-server(사실상 표준)** 수준에서 제공 가능한 진단 기능 5종. 각각 krew 인기 플러그인으로 수요가 검증됐고, 기존 TUI에서는 제공되지 않았으며, 에이전트 시너지가 높은 것만 선별했다.

| # | 기능 | 수요 증거 | 사용 API | 단계 |
|---|---|---|---|---|
| 1 | **이벤트 인텔리전스** | 기존 TUI 이벤트 뷰는 단순 목록 수준. 트러블슈팅 1순위 정보원 | core v1 Events (`fieldSelector`, watch) | **MVP** |
| 2 | **소유권 트리** | kubectl-tree 3.4k⭐ + kube-lineage 0.5k⭐ | `ownerReferences` 재귀 탐색 | **MVP** |
| 3 | **RBAC 분석** | rakkess 1.4k⭐ + who-can 0.9k⭐ + rbac-lookup 1.0k⭐ | `SubjectAccessReview`, `SelfSubjectRulesReview`, Role/Binding | Phase 2 |
| 4 | **사용량 vs Requests/Limits** | kube-capacity 2.7k⭐ | `metrics.k8s.io/v1beta1` + Pod spec resources | Phase 2 |
| 5 | **PDB/Quota 인식 + drain 시뮬레이션** | 도구 공백 지점, SRE 필수 작업 | `policy/v1` PDB, ResourceQuota, LimitRange | Phase 2 |

**설계 포인트:**

- **이벤트 인텔리전스**: 오브젝트 컨텍스트 이벤트 뷰(선택 리소스의 이벤트만 `fieldSelector` 필터), Warning 타임라인, 반복 패턴 배지(count 기반). 에이전트가 비정형 `.message`를 해석하는 **LLM 최대 강점 영역** — "지난 30분 Warning 요약"이 즉시 가치를 낸다. 소유권 트리와 결합해 장애 타임라인 자동 구성
- **소유권 트리**: MVP는 순방향만(Deployment→RS→Pod, ownerReferences 필드 그대로). 역방향 인덱싱(전체 리소스 스캔)은 Phase 2. **에이전트 컨텍스트 공급 장치** — 진단 시 선택 리소스의 소유권 체인을 자동으로 시스템 컨텍스트에 포함해 진단 정확도를 올림
- **RBAC 분석**: "내 권한 목록"(SelfSubjectRulesReview, 1콜)부터 시작. 에이전트 write 도구 실행 전 `SubjectAccessReview`로 **승인 게이트에서 권한 사전 검증** — 실패 시 "권한 부족: {verb} {resource}"를 승인 다이얼로그 단계에서 알림(§5 #5와 동일 원칙, §6.4 RBAC 사전 검증의 일반화)
- **사용량 vs Requests/Limits**: pod/node 뷰에 usage/requests/limits 3열 + 퍼센티지 바. 오버프로비저닝 감지(실사용 ≪ requests). metrics-server 미설치 시 usage 열만 비활성 + 사유 표시 (graceful degradation)
- **PDB/drain 시뮬레이션**: node 뷰에서 drain 전 "이 노드의 pod ↔ PDB 매칭 → disruptionsAllowed 검사" 결과를 승인 다이얼로그에 표시. **승인 게이트의 가장 구체적인 활용 사례**. ResourceQuota 잔량 게이지는 ns 뷰에 표시

**에이전트 도구 추가분** (§6.2 테이블 확장):

| 도구 | 그룹 | 게이트 |
|---|---|---|
| `get_owner_chain`, `get_object_events`, `summarize_events` | k8s read | 없음 |
| `check_access` (SubjectAccessReview), `list_my_permissions` | k8s read | 없음 |
| `analyze_resource_usage` (metrics + spec 비교) | k8s read | 없음 |
| `simulate_drain` (PDB 위반 사전 검사) | k8s read | 없음 (drain 실행 자체는 write 게이트) |

**검토했으나 제외한 것**: rollout 관리(기존 도구들이 이미 잘 제공, 차별화 여지 적음), TUI 인라인 YAML 에디터(난이도 高 — dry-run diff는 승인 다이얼로그에 이미 포함), NetworkPolicy 시뮬레이션(바닐라 API는 "허용 규칙"만 보여주고 실제 플로우 불가, CNI별 enforcement 상이), 다중 port-forward 관리(asyncio+SPDY+Textual 기술 리스크, LLM 시너지 낮음 — 단일 포워딩은 Phase 2 유지).

---

## 7. 데이터 계층

- **WatchManager**: 뷰가 구독하는 (group, version, kind, ns) 단위로 watch 태스크 생성/공유/해제. 화면에 없는 리소스는 watch하지 않음(§5 #6). 재연결은 지수 백오프 + resourceVersion 북마크
- **ResourceStore**: watch 이벤트를 반영하는 인메모리 캐시. UI는 스토어의 reactive 스냅샷만 구독 (데이터↔렌더링 분리, §5 #10/#11)
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
| **Phase 1 — MVP** | pods/deploy/svc/events/ns/ctx 뷰, `:` 팔레트, `/` 필터, 2-pane 분할, 1급 로그 뷰어(멀티-pod/JSON/재연결), 계층형 안전장치, RBAC 에러 매핑, 선택적 watch, 단일 config, 키바인딩 오버라이드, **에이전트 패널(read 도구 + UI 제어 + 승인 기반 kubectl write)**, **라이브 디버깅(ephemeral container + 에이전트 debug 도구, §6.4)**, **이벤트 인텔리전스·소유권 트리(순방향, §6.5)**, 감사 로그 | korvid 단독으로 일상 진단 워크플로 수행 가능 |
| **Phase 2** | 전체 리소스+CRD 자동 감지, shell-in, 포트포워드, copy-of-pod/node debug, Secret 디코드 편집, 메트릭(top) 정렬, **RBAC 분석·사용량 vs Req/Limits·PDB/drain 시뮬레이션(§6.5)**, 소유권 트리 역방향 인덱싱, 커맨드 팔레트 발견성, 세션 상태 복원, anonymize | 일상 클러스터 운영 전반을 단독 커버 |
| **Phase 3** | Python 플러그인 API(패널/컬럼/에이전트 도구 등록), 외부 MCP 서버 연결(에이전트 도구 확장), 멀티 클러스터 동시 뷰, 임베디드 디버그 터미널 패널, 진단 플레이북, (검토) Cilium/Hubble 플로우 뷰 | 생태계 확장 개시 |
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
5. **(검토 수준) Cilium/Hubble 네트워크 플로우 뷰** — 기술 타당성 확인됨: Hubble Relay gRPC(`GetFlows` 스트림, 포트포워드 후 insecure 접속 가능), proto에서 Python stub 생성 가능, Hubble UI 서비스맵도 플로우 집계 방식이라 동일 접근 가능. TUI에서 네트워크 토폴로지를 그린 기존 도구 전무(차별화 기회). 단 Hubble 미설치 fallback·TLS·proto 유지보수 리스크가 있어 Phase 3 검토 항목으로만 유지 (플러그인 API의 1호 후보)

## 13. 기존 도구 보완개발 vs 신규 개발 비용 검토

이 스코프를 실현하는 세 가지 경로를 비용 관점에서 비교했다.

### 경로별 비용 구조

| | A. 업스트림 기여 (k9s에 PR) | B. 포크 후 개조 | **C. 신규 개발 (채택)** |
|---|---|---|---|
| 초기 비용 | 낮아 보이나 불확실성 최대 | 온보딩: 34k★ 규모 Go 코드베이스 이해 | 전체 신규 작성 |
| 아키텍처 적합성 | 에이전트 패널·UI 운전은 코어 구조 변경 필요 — 플러그인(shell-out)으로 불가 | 단일-뷰 스택(tview PageStack) → 분할 패널 전환은 렌더링 계층 전면 개조. UI Bus·승인 게이트도 기존 이벤트 처리와 직교하지 않아 코어 재설계 | 목표 아키텍처(UI Bus, 에이전트 통합, 분할 패널)에 처음부터 최적화 |
| 재사용 가능 자산 | — | 데이터 계층(informer 래핑) 정도. AI 루프·어댑터·승인/감사 체계는 어차피 전부 신규 | Textual이 분할 패널·async·테마·테스트 러너를 프레임워크 수준에서 제공 — 기존 도구들이 직접 구현해야 했던 UI 인프라 상당 부분이 무료 |
| 지속 비용 | 머지 여부·시점을 통제 불가 (선행 AI 통합 PR 2건이 머지에 이르지 못한 전례) | 업스트림과 계속 diverge → 보안 패치·기능 머지 비용이 영구 발생. "포크"라는 포지셔닝 부담 | 유지보수 전체 소유. 성숙도 축적(엣지 케이스)은 시간 필요 |
| 언어/역량 | Go | Go (LLM 생태계 라이브러리는 Python 대비 상대적으로 얇음) | Python — Textual·LLM 생태계 모두 강함 |

### 판정: C (신규 개발)

핵심 근거는 **"이 프로젝트의 차별화 요소가 기존 아키텍처와 직교하지 않는다"**는 점이다:

1. 에이전트가 TUI를 운전하려면 모든 UI 액션이 커맨드 버스를 통과해야 하는데(§4.1), 이는 부가 기능이 아니라 **중심 설계**다. 기존 코드베이스에 이식하면 사실상 코어 재작성이 된다 → B는 "포크 후 재작성"으로 수렴하며, 온보딩+개조+영구 diverge 비용이 신규 개발보다 총비용이 높다
2. A(기여)는 프로젝트 방향 결정권이 없어 이 스코프의 실현 가능성 자체가 통제 불가
3. 신규 개발의 최대 리스크(UI 인프라 구현량)는 Textual 프레임워크가 상당 부분 흡수하고, 나머지 리스크(성숙도)는 §5의 실측된 요구 목록을 테스트 시나리오로 삼아 단계적으로 관리
4. 신규 개발이더라도 **검증된 UX 관습은 계승**(§5 마지막 절)해 사용자 전환 비용은 포크 수준으로 낮게 유지

단, 신규 개발 선택이 기존 생태계와의 단절을 의미하지 않는다 — §5의 요구 실측은 기존 도구 커뮤니티가 축적한 지식 위에 서 있고, Phase 3 플러그인 API·MCP 연동으로 생태계와 상호운용을 지향한다.
