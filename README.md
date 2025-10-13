# Ainux

Ainux is an AI-native operating system concept that layers intelligent
automation and hardware orchestration on top of a familiar Linux user
experience. This repository now contains both the high-level architecture
vision and the initial tooling required to assemble an Ubuntu-based live ISO
that demonstrates those ideas.

## Repository Structure

- `docs/ai_friendly_os_design.md` – Design document outlining the Ainux vision
  and automation architecture.
- `build/ubuntu-ainux/` – Scripts and configuration to generate an Ubuntu
  remix ISO with Ainux defaults baked in, including AI-driven maintenance,
  scheduling, and network automation helpers.
- `ainux_ai/` – Python GPT client, 자연어 오케스트레이터, 컨텍스트 패브릭, 지능형
  하드웨어 자동화 툴킷, 그리고 OpenAI 호환 API와 통신하는 CLI.

## Quick Start

To build the prototype ISO:

```bash
git clone https://github.com/<your-org>/Ainux.git
cd Ainux/build/ubuntu-ainux
sudo ./build.sh --release jammy --arch amd64 --output ~/ainux-jammy.iso
```

Refer to `build/ubuntu-ainux/README.md` for prerequisites and customization
options, including the new scheduling/packet-management blueprints seeded into
the live image.

## Configuring GPT access

The repository ships with a reusable CLI (`ainux-ai-chat`) and Python module
(`ainux_ai`) that connect to GPT-style APIs. Configure a provider once and both
the live ISO and host tooling can reuse the credentials:

```bash
# Configure an OpenAI account and make it the default provider
python -m ainux_ai configure --api-key sk-... --default

# Update or rotate the API key later without changing other settings
python -m ainux_ai set-key --api-key sk-new-...

# Send a quick prompt
python -m ainux_ai chat --message "Ainux에 대해 한 문장으로 요약해줘"

# Use environment variables for ephemeral sessions
AINUX_GPT_API_KEY=sk-... AINUX_GPT_MODEL=gpt-4o-mini python -m ainux_ai chat --message "hello"
```

Inside the live ISO the `ainux` user can run `ainux-ai-chat chat --interactive`
to hold multi-turn conversations, switch between multiple saved providers, and
log transcripts for auditing.

## 자연어 오케스트레이션 사용하기

`ainux-ai-chat orchestrate` 서브커맨드는 자연어 요청을 인텐트 → 실행 계획 →
안전성 검토 → (선택적) 실행 단계로 이어지는 파이프라인에 연결합니다. GPT
제공자를 설정하면 모델이 계획을 도와주고, 제공자가 없거나 `--offline`
플래그를 사용하면 휴리스틱 모드로 동작합니다.

```bash
# GPT 제공자를 활용하여 GPU 드라이버 갱신 계획을 생성하고 드라이런합니다.
python -m ainux_ai orchestrate "CUDA랑 GPU 드라이버 최신 버전으로 맞춰줘" --dry-run

# 컨텍스트 JSON을 전달하여 유지보수 대상 정보를 함께 넘길 수도 있습니다.
python -m ainux_ai orchestrate "금요일 21시에 추론 서버 네트워크 점검 예약해줘" \
  --context maint_window.json
```

명령어는 인텐트, 단계별 계획, 안전성 경고, 실행 로그를 콘솔에 요약하며
`--json` 플래그로 구조화된 출력을 받을 수 있습니다. 기본 레지스트리는
드라이런/청사진 기록 중심으로 구성되어 있으므로 실제 인프라 자동화에
맞게 커스텀 기능을 확장할 수 있습니다.

## 컨텍스트 패브릭 활용하기

`ainux-ai-chat context` 서브커맨드는 파일, 설정, 이벤트를 지식 그래프와
이벤트 버스로 수집하여 오케스트레이터가 참조할 수 있는 공통 상태를
만듭니다. CLI에서 즉시 스냅샷을 살펴보고 새로운 정보를 주입할 수
있습니다.

```bash
# 설계 문서를 그래프에 등록하고 태그를 달기
python -m ainux_ai context ingest-file docs/ai_friendly_os_design.md \
  --label "Architecture spec" --tag design --tag docs

# 오케스트레이터 기본 모드를 설정 스코프에 기록
python -m ainux_ai context ingest-setting orchestrator.mode assist --scope user

# 유지보수 이벤트를 남기고 최근 상태를 확인
python -m ainux_ai context record-event maintenance.started \
  --data '{"target": "gpu-fleet"}'
python -m ainux_ai context snapshot --limit-events 5

# 자연어 오케스트레이션에 컨텍스트 패브릭 스냅샷을 병합
python -m ainux_ai orchestrate "토요일 02시에 GPU 점검 예약" --use-fabric
```

스냅샷은 `~/.config/ainux/context_fabric.json`에 저장되며, `--fabric-path`
옵션으로 경로를 재정의할 수 있습니다. 오케스트레이터는 `--use-fabric`
또는 사용자 지정 경로가 지정되면 요청/계획/실행 결과를 자동으로 이벤트로
기록합니다.

## 지능형 하드웨어 자동화

`ainux_ai.hardware` 패키지와 `ainux-ai-chat hardware` 서브커맨드는 드라이버·
펌웨어 카탈로그, 의존성 그래프, 텔레메트리 수집을 하나로 묶어 GPU/가속기
자동화를 실행합니다. 컨텍스트 패브릭을 사용하면 스캔과 실행 로그가 자동으로
지식 그래프와 이벤트 버스에 기록됩니다.

```bash
# 현재 시스템 하드웨어를 스캔하고 카탈로그에 저장
python -m ainux_ai hardware scan

# 드라이버/펌웨어 블루프린트 확인 및 추가
python -m ainux_ai hardware catalog show
python -m ainux_ai hardware catalog add-driver nvidia-driver 535 --package nvidia-driver-535 \
  --package nvidia-dkms-535 --module nvidia --vendor nvidia --supports 10de:1eb8

# 감지된 컴포넌트를 기준으로 설치 계획 생성 (JSON 출력)
python -m ainux_ai hardware plan --json

# 텔레메트리 스냅샷을 3회 수집하고 패브릭 이벤트로 남기기
python -m ainux_ai hardware telemetry --samples 3 --interval 2
```

`--catalog-path`로 카탈로그 저장 위치를, `--fabric-path`로 패브릭 경로를
재정의할 수 있으며, `--no-fabric`을 지정하면 이벤트 로깅 없이 독립적으로
동작합니다. `hardware plan --apply`는 생성된 단계를 실제로 실행하며,
`--dry-run`과 함께 사용하면 명령어만 미리 확인할 수 있습니다.

## 브라우저 오케스트레이션 스튜디오

터미널만으로는 자연어 흐름과 실행 로그를 한눈에 보기 어렵기 때문에,
`ainux_ai.ui` 패키지는 글래스모피즘 테마의 웹 UI를 제공합니다. 아래
명령으로 로컬 서버를 띄우면 기본 브라우저가 열리며, 좌측 패널은 대화형
자연어 타임라인을, 우측 패널은 계획·명령 로그·컨텍스트 패브릭 메타데이터를
실시간으로 갱신합니다.

```bash
# 기본 설정: 드라이런 모드 + 컨텍스트 패브릭 활성화
python -m ainux_ai ui

# GPU 작업을 즉시 실행하고 싶다면 --execute를 명시
python -m ainux_ai ui --execute --provider openai

# 서버 환경에서 브라우저 없이 띄우고 싶다면 --no-browser 사용
python -m ainux_ai ui --host 0.0.0.0 --port 9000 --no-browser
```

UI 내 토글을 통해 드라이런/실행, 오프라인 모드, 컨텍스트 패브릭 사용 여부를
즉시 바꿀 수 있으며, 프롬프트 제출 시 오케스트레이터 결과와 계획 단계, 실행
출력, 최신 패브릭 이벤트가 카드 형태로 정리됩니다. GPT 제공자가 설정되지
않았거나 오류가 발생하면 경고 배지가 표시되고 휴리스틱 모드로 자동
폴백합니다.

## Current Status

Ainux is presently a concept prototype: the repository contains architecture
documentation plus tooling to assemble an Ubuntu-based ISO with automation
helpers preinstalled. The GPT connector, orchestration runtime, and context
fabric deliver an initial natural-language → plan → execution loop that can
operate with or without model assistance. Richer UI surfaces, advanced
contextual reasoning, and deep hardware integrations described in the design
guide remain in development.
