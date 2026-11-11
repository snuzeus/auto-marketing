# GEMINI.md / CLAUDE.md — Progressive Context Router & Modular Specification

> **\[수석 아키텍트 설계 노트: 왜 계층형 분할 명세(Progressive Context Disclosure)인가?\]**
> Claude Code, Gemini CLI, Cursor 등 모든 최신 코딩 에이전트는 프로젝트 루트의 `CLAUDE.md` / `GEMINI.md`를 **매 턴(Every Turn) 시스템 프롬프트에 자동 주입**합니다.
> 만약 1,000줄짜리 상세 코드 스펙을 루트 파일 하나에 몰아넣으면, 단순 파일 수정이나 테스트 실행 턴마다 매번 약 12,000\~15,000 토큰이 중복 과금되며 모델의 주의력 분산(Attention Dilution)이 발생합니다.
>
> 따라서 본 문서는 실무 엔지니어링 표준인 **2-Tier 계층형 컨텍스트 아키텍처**로 설계되었습니다:
>
> 1. **Root `CLAUDE.md` / `GEMINI.md` (Part 1, 약 110줄):** 상시 로드되는 **경량 글로벌 라우터(1,200 토큰 미만)**. 전체 아키텍처 원칙, 2-Tier 모델 정책, **폴더별 `SPEC.md` 라우팅 테이블**, 단계별 구현 순서(DAG)만 포함합니다.
>
> 2. **Folder-Level `SPEC.md` (Part 2, 지연 로드 / On-Demand):** 각 폴더(`config/`, `src/core/`, `src/agents/`, `src/mcp/`, `src/integrations/`)에 격리 저장되는 상세 구현 명세서입니다. 코딩 에이전트는 **현재 작업 중인 폴더의 `SPEC.md` 단 1개만 읽고(Lazy Load)** 코드를 작성합니다.

## 0. \[최초 1회 실행\] Zero-Token 폴더별 명세서 자동 분할 부트스트랩

이 파일을 프로젝트 루트에 `CLAUDE.md`(또는 `GEMINI.md`)로 저장한 뒤, 코딩 에이전트를 실행하기 전에 아래 **Python 부트스트랩 명령어를 터미널에서 1회 실행**하십시오. **LLM 토큰을 단 1개도 소모하지 않고** 각 하위 폴더에 `SPEC.md` 5개를 자동 생성하며, 루트 문서는 110줄짜리 경량 라우터(Part 1)만 남기고 깔끔하게 슬림화합니다.

```
python3 -c '
import pathlib, re

doc_path = pathlib.Path("CLAUDE.md") if pathlib.Path("CLAUDE.md").exists() else pathlib.Path("GEMINI.md")
text = doc_path.read_text(encoding="utf-8")

# 1. Part 2의 각 모듈별 SPEC 블록을 추출하여 해당 폴더에 파일로 생성
pattern = re.compile(r"<!-- FILE_START: (.+?) -->\n(.*?)\n<!-- FILE_END -->", re.DOTALL)
for rel_path, content in pattern.findall(text):
    target = pathlib.Path(rel_path.strip())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.strip() + "\n", encoding="utf-8")
    print(f"[Created] {target}")

# 2. 루트 CLAUDE.md / GEMINI.md는 <!-- ROOT_SPEC_END --> 이전의 경량 라우터만 남겨 덮어쓰기
root_only = text.split("<!-- ROOT_SPEC_END -->")[0].strip() + "\n"
doc_path.write_text(root_only, encoding="utf-8")
print(f"[Slimmed Root] {doc_path} ({len(root_only.splitlines())} lines)")
'

```

*(만약 터미널 명령어 대신 에이전트에게 직접 분할을 시키려면 첫 프롬프트로 `"CLAUDE.md의 0번 부트스트랩 스크립트를 실행해서 폴더별 SPEC.md를 분리하고 시작해줘"`라고 입력하십시오.)*

## 1. 프로젝트 핵심 개요 (Global Project Overview)

* **목표:** 단일 이미지(`사진 1장`)와 간단한 프롬프트 입력만으로 **Instagram**, **X (Twitter)**, **디시인사이드**, **네이버 카페** 중 사용자가 선택한 플랫폼 조합(`enabled_channels`)에 맞춰 홍보 콘텐츠를 자동 생성·검증(Self-Correction)하고, Human-in-the-Loop(관리자 승인)를 거쳐 발행하는 Agentic AI 파이프라인.

* **코어 런타임:** Python 3.12+, `asyncio`, `Pydantic v2`, `google-genai` (최신 공식 SDK), `mcp` (`FastMCP`), `httpx`, `Pillow`, `uv`

* **토큰 가성비 최적화 2-Tier LLM 엔진 (Sonnet + Haiku 대응 구조):**

  * **Main Workhorse (`gemini-3.8-flash`):** 멀티모달 비전 분석(`VisionAgent`), 채널별 카피라이팅(`CopyAgent`), MCP 도구 호출 전담. 초안 생성 시 `thinking_level="low"`를 강제하여 불필요한 추론(Thinking) 토큰 과금을 차단(`gemini-4-argon` 대비 약 81% 비용 절감).

  * **Ultra-Fast Subagent (`gemini-3.5-flash-lite`):** 한/영/일 및 네이버 카페 C-Rank 해시태그 추출(`TagEngine`), 2-Stage 시맨틱 규격 검증(`ValidatorAgent`) 전담.

## 2. 컨텍스트 라우팅 테이블 (Context Routing Table — Mandatory Agent Rule)

코딩 에이전트는 토큰 절약을 위해 **한 번에 하나의 모듈 그룹만 구현**하며, 작업 시작 전 반드시 아래 표에 매핑된 **해당 폴더의 `SPEC.md` 파일만 선택적으로 `read_file`** 해야 합니다. 다른 폴더의 `SPEC.md`를 미리 읽지 마십시오.

| 구현 단계 (Phase) | 작업 대상 디렉토리 및 파일 | 필수 참조 하위 명세서 (On-Demand Load) | 핵심 구현 목표 | 
| ----- | ----- | ----- | ----- | 
| **Phase 1** | `config/personas.yaml`, `.env.example`, `docker-compose.yml` | `config/SPEC.md` | 동적 플랫폼 토글(`active_channels`), 4개 채널 페르소나, Postiz 인프라 설정 | 
| **Phase 2** | `src/core/config_loader.py`, `src/core/schema.py`, `src/core/llm_client.py` | `src/core/SPEC.md` | 동적 채널 리졸버, Pydantic v2 `SocialPostOutput`, `google-genai` 래퍼 | 
| **Phase 3** | `src/agents/vision_agent.py`, `tag_engine.py`, `copy_agent.py`, `validator_agent.py` | `src/agents/SPEC.md` | 4-Block 프롬프팅, `thinking_level="low"`, 최대 3회 Self-Correction 루프 | 
| **Phase 4** | `src/integrations/postiz_client.py`, `naver_cafe_client.py`, `dcinside_router.py` | `src/integrations/SPEC.md` | Postiz Draft API, 네이버 카페 OAuth2/Multipart API, 텔레그램/클립보드 라우터 | 
| **Phase 5** | `src/mcp/server.py`, `src/mcp/tools/*.py`, `src/main.py`, `tests/` | `src/mcp/SPEC.md` | Gemini `FunctionDeclaration` & MCP 도구 바인딩, CLI 엔드투엔드 오케스트레이터 | 

## 3. 아키텍처 데이터 흐름 요약 (High-Level Pipeline Flow)

```
flowchart LR
    Input["이미지 1장 + 프롬프트\n+ --channels 선택"] --> Config["0. ConfigLoader\nenabled_channels 확정"]
    Config --> Vision["1. VisionAgent\n(gemini-3.8-flash)"]
    Vision --> Tag["2. TagEngine\n(gemini-3.5-flash-lite)"]
    Vision --> Copy["3. CopyAgent\n(gemini-3.8-flash)"]
    Tag --> Copy
    Copy --> Validator{"4. ValidatorAgent\n(Self-Correction <= 3회)"}
    Validator -->|Fail| Copy
    Validator -->|Pass| MCP["5. MCP Dispatcher\n(활성 채널 도구만 호출)"]
    MCP --> Out1["Postiz Draft\n(Instagram / X)"]
    MCP --> Out2["Naver Cafe API\n(텔레그램 승인 후 등록)"]
    MCP --> Out3["DCInside Pack\n(1초 클립보드 + 텔레그램)"]

```

## 4. 전역 코딩 헌법 (Global Engineering Guardrails)

1. **엄격한 SDK 준수:** 레거시 `google-generativeai` 사용을 금지하며, 반드시 `from google import genai` (`google-genai` SDK)의 비동기 인터페이스(`client.aio.models.generate_content`, `client.aio.caches.create`)만 사용합니다.

2. **동적 플랫폼 격리 (`enabled_channels`):** 사용자가 비활성화한 플랫폼은 프롬프트 생성, Pydantic 검증, MCP Tool 호출 어디에서도 실행되지 않아야 합니다 (`SocialPostOutput`에서 비활성 채널 필드는 `None` 유지).

3. **Human-in-the-Loop 보장:** 어떠한 도구도 관리자 승인 없이 SNS에 즉시 공개 발행(`PUBLISHED`)하지 않습니다. 인스타/X는 Postiz `DRAFT` 상태로만 저장하고, 네이버 카페는 텔레그램 승인 콜백 또는 클립보드 모드를 거칩니다.

4. **코드 품질 게이트:** 각 Phase 구현 직후 아래 명령어를 실행하여 무결성을 검증합니다.

   * 린트 & 타입체크: `uv run ruff check src/ tests/ --fix && uv run mypy src/ --strict`

   * 단위 테스트: `uv run pytest -v --asyncio-mode=auto`

   * 선택 플랫폼 실행: `uv run python src/main.py --image test.jpg --prompt "소개 문구" --channels instagram,naver_cafe --dry-run`
