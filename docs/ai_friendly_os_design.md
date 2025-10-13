# Ainux: AI-Friendly Operating System Concept

## Vision
Ainux aims to be an AI-native desktop operating system where natural language
requests can drive full-system automation. Users describe intentions, and the
OS orchestrates the right applications, services, and workflows to complete
those tasks autonomously while keeping the user in control.

> **Implementation status:** The current prototype focuses on infrastructure
> automation scaffolding and now ships with a configurable GPT connector
> (`ainux-ai-chat`), a baseline intent→plan→execution orchestrator, the context
> fabric module for 파일/설정/이벤트 그래프화, 지능형 하드웨어 자동화
> (`ainux_ai.hardware`) that catalogs devices/드라이버/펌웨어와 텔레메트리를
> 관리하며, 인프라 스케줄링/네트워크/클러스터 헬스를 담당하는
> `ainux_ai.infrastructure` 서비스(`scheduler`, `network`, `cluster` 서브커맨드),
> 그리고 자연어·플랜·실행 로그를 한 화면에 묶어 주는 브라우저 UI
> (`python -m ainux_ai ui`). Richer conversational planners, governance &
> security controls, and the immersive UI capabilities described below remain
> in development and are not yet fully integrated into the Ubuntu remix.

## Design Principles
- **User-first autonomy**: The AI should execute multi-step tasks on the
  user's behalf but surface plans, confirmations, and outcomes at every
  critical decision point.
- **Contextual awareness**: Maintain rich state about active files,
  applications, and user preferences so that AI reasoning always has the right
  context.
- **Composable intelligence**: Combine deterministic system services with
  LLM-based planning so that actions remain reliable, explainable, and
  repeatable.
- **Security & privacy by default**: Use sandboxed execution, fine-grained
  permission prompts, and data minimization to keep user data safe.
- **Transparency**: Log every AI-generated action with rationale so users can
  audit and refine future behavior.

## High-Level Architecture
1. **AI Orchestration Layer**
   - Intent parser converts natural language commands into structured tasks.
   - Planner decomposes tasks into ordered system actions with fallbacks.
   - Critic/safety module evaluates plans for policy or permission violations
     before execution.
2. **Context Fabric**
   - Unified knowledge graph indexing files, system settings, application
     states, and recent interactions.
   - Event bus streams updates (file changes, notifications, sensor data) to
     keep AI models synchronized with reality.
   - **현재 구현:** `ainux_ai.context` 패키지가 `ContextFabric` 클래스를 제공하여
     지식 그래프/이벤트 버스를 저장하고, `ainux-ai-chat context` CLI를 통해 파일·
     설정·이벤트를 기록하거나 스냅샷을 추출할 수 있다.
3. **Action Execution Engine**
   - Library of capability adapters that wrap OS APIs, CLI tools, and app
     automations.
   - Transaction manager ensures multi-step operations can roll back safely on
     failure.
   - Hardware control plane negotiates with device managers (GPU, storage,
     peripherals) and mediates firmware/driver updates.
   - **현재 구현:** `ainux_ai.hardware` 서브모듈이 하드웨어 인벤토리 스캔,
     드라이버/펌웨어 카탈로그, 의존성 그래프 기반 설치 계획, 텔레메트리 수집,
     컨텍스트 패브릭 이벤트 로깅을 제공하며 `ainux-ai-chat hardware` CLI로
     노출된다. `ainux_ai.infrastructure`는 유지보수 블루프린트/정비 윈도우,
     SLURM 작업, 네트워크 프로파일(QoS·VLAN·nftables), 클러스터 헬스 스냅샷을
     관리하는 서비스 집합을 제공하고 `ainux-ai-chat scheduler|network|cluster`
     명령으로 접근할 수 있다.
4. **Human Feedback Interface**
   - Conversational UI blending chat, voice, and visual prompts.
   - Explanation panels showing the planned steps, required permissions, and
     live progress updates.
- **현재 구현:** 글래스모피즘 스타일의 웹 스튜디오가 자연어 타임라인, 계획 카드,
  실행 로그, 컨텍스트 패브릭 이벤트를 동시에 보여 주며 토글로 드라이런/오프라인
  모드를 제어할 수 있다. 0.7 업데이트에서는 정사각형 Ainux 로고와 펭귄 마스코트를
  히어로 배경/마스코트 패널에 배치하여 브랜드 일관성을 확보했고, `/usr/share/ainux/branding`
  경로의 이미지를 교체하면 테마가 바로 반영되도록 구성했다. 음성 입력, 다중 세션,
  협업 뷰는 추후 계획이다.

## Intelligent Hardware & Runtime Management
- **Hardware intent abstraction**: Users can request high-level goals such as
  "CUDA 환경을 준비해줘" or "신규 GPU 드라이버로 업그레이드해줘" and Ainux
  translates them into deterministic provisioning steps.
- **Driver & firmware automation**: The OS maintains a catalog of compatible
  driver, CUDA, cuDNN, and firmware versions per device, validates integrity,
  and stages updates in sandboxes before cutover.
- **Dependency graph reasoning**: Installation plans account for kernel
  modules, container runtimes, and framework bindings so mismatched versions
  are detected ahead of time.
- **Device health telemetry**: Continuous monitoring of thermals, error rates,
  and utilization feeds back into the Context Fabric so AI agents can suggest
  optimizations or pre-emptive maintenance.
- **Provisioning blueprints**: Reusable templates capture best-practice GPU
  setups (e.g., multi-GPU training, mixed-precision inference) and can be
  shared via the automation marketplace for one-click replication across
  machines or teams.

## AI-Native Infrastructure Scheduling
- **Service orchestration**: Dedicated AI agents understand higher-level intents
  such as "신규 추론 서버 두 대로 확장해줘" or "금요일 저녁에 펌웨어 업데이트
  스케줄링해줘", mapping them to declarative blueprints that coordinate
  provisioning, maintenance windows, and rollback policies.
- **Hardware-aware scheduling**: The planner reasons about GPU topology,
  NUMA layout, network bandwidth, and power budgets so long-running training
  jobs or inference services are placed where latency and thermal thresholds
  are satisfied.
- **Network & packet automation**: AI capabilities manage VLAN creation,
  firewall policies, and traffic shaping rules, integrating telemetry feedback
  to adapt QoS settings as workloads evolve.
- **Multi-tenant policies**: Business environments can define guardrails for
  resource quotas, change windows, and approval chains so that AI-driven
  actions meet compliance requirements without manual babysitting.
- **Scenario memory**: Completed scheduling runs are logged with context,
  outcomes, and adjustments, enabling fast replays or incremental updates the
  next time a similar workflow is requested.

### Domain-Specific Command Surface
- **Maintenance DSL**: `ainux-scheduler` executes human-readable maintenance
  and provisioning recipes, automatically translating AI plans into Ansible
  blueprints or SLURM submissions.
- **Network authority**: `ainux-network-orchestrator` keeps packet shaping,
  VLAN updates, and firewall adjustments auditable while still allowing the AI
  to adapt rules in real time.
- **Operations telemetry**: `ainux-cluster-health` aggregates GPU, BMC, and job
  scheduler signals so the planning agents receive immediate feedback after
  every change.

## Operating Modes
- **Assist Mode**: AI drafts plans and awaits explicit approval before each
  execution.
- **Autopilot Mode**: AI proceeds automatically within a user-defined scope
  (time window, specific apps) and only interrupts on errors or escalations.
- **Training Mode**: Users walk through workflows manually while the system
  records them as automations that the AI can later replay and adapt.

## Workflow Lifecycle
1. Capture user intent (text/voice) and snapshot relevant context.
2. Generate and simulate the execution plan.
3. Request permissions where needed and log approvals.
4. Execute actions, streaming updates and handling exceptions.
5. Summarize outcomes, collect feedback, and learn adjustments for next time.

## Safety & Governance
- Permission model tied to identity, device posture, and data sensitivity.
- Policy engine to enforce organizational or parental controls on AI actions.
- Immutable audit trail with explainability metadata for each automation.
- Offline/air-gapped inference option for sensitive environments.

## Extensibility
- Plugin SDK so developers can expose new capabilities with declarative
  schemas describing inputs, outputs, and safety constraints.
- Model abstraction layer supporting multiple LLM providers and local models.
- Automation marketplace for sharing vetted workflows.

## Example Scenarios
- "정리 안 된 다운로드 폴더를 폴더별로 정리해줘" → AI groups files by type,
  creates folders, confirms summary, and executes organization.
- "금요일 회의 준비해줘" → Gathers documents, drafts agenda, schedules calendar
  reminders, and assembles a briefing package.
- "새로운 개발 환경 만들어줘" → Provisions containers, installs dependencies,
  configures editors, and verifies setup with smoke tests.
- "CUDA랑 GPU 드라이버 최신 버전으로 맞춰줘" → Detects hardware model,
  selects validated driver/CUDA combo, snapshots the system, applies updates,
  runs verification tests, and rolls back automatically if anomalies appear.
- "금요일 21시에 추론 서버 네트워크 점검 예약해줘" → Plans maintenance window,
  drains traffic via load balancer rules, applies packet filter updates, and
  verifies service health before returning to normal routing.

## Roadmap Highlights
- MVP: Natural language command shell with deterministic action templates.
- Beta: Context fabric integration, audit logging, plugin SDK, guided hardware
  provisioning workflows for GPUs/accelerators, and AI-driven maintenance
  scheduling with approval routing.
- 1.0: Full autopilot workflows with adaptive learning, enterprise governance
  features, and policy-aware infrastructure orchestration across fleets.
