"""한국어 추출용 런타임 패치 (테스트 전용 — site-packages 는 건드리지 않음).

코드로 확인한 두 가지 (2026-11-24)
  1) **언어 충돌**
     `generate_additive_extraction_prompt()` 가 유저 메시지에
     "CRITICAL: Always write all extracted memory text in English … Translate any
     non-English content into English" 를 매번 넣는다. system 쪽 `_LANGUAGE_RULE`
     ("SAME language the user spoke") 보다 뒤에 오므로 실질 지시는 영어 번역이다.
     한국어 발화를 영어로 번역해 저장하면 로컬 E5 검색에서 한국어 질의와 멀어진다
     (측정: vm_probe_extract.py --full → 저장 문장이 전부 영어).
  2) **시간어 정규식이 영어 전용**
     충돌 판정(ConflictResolver)과 검색 재정렬 보너스(`_DURATION_RE/_DATE_RE`)가
     "two years"/"March 3, 2024" 같은 영어 표현만 인식한다. 번역본에는 그 표현이
     있을 수 있어도, 원문 유지로 바꾸는 순간 한국어 시간 표현이 통째로 사라진다.

적용 내용
  - 유저 프롬프트의 `## Language Requirement` 블록 → 항상-한국어 유지 규칙
  - 추출 호출에 max_tokens 상한 + timeout 상한
    (사고형 모델 qwen3:1.7b 가 60초 기본 timeout 을 넘겨 APITimeoutError, 실측 182초)
  - (옵션 VM_KO_REPLACE_WITH_ORIGINAL=1) prompt 에 원문과 똑같이 나온 사실은
    번역본 대신 **원문 그대로** 저장 — 추출기가 일부 문장만 번역했을 때 구제용.
    기본은 꺼 둔다: 원문 한 문장에 사실이 여러 개 섞여 있을 때(예: 이름+거주지)
    통째로 덮어써 1건으로 뭉개질 수 있다.

검색 쪽 한국어 시간어 보강은 `vm_ko_search_patch.py` 가 따로 담당한다.

**정정 (2026-11-24 실측 후)**
  위 1) 은 "프롬프트에 고정으로 박혀 있다"는 뜻이 아니다. `mem0_additive_prompt_build.py:98-116`
  은 `use_input_language=True` 면 한국어 유지, False 면 영어 번역 규칙을 넣는다.
  실제로 저장된 결과를 보니 **한국어 원문 1건 + 영어 번역 2건이 같이 남았다**(중복).
  → 따라서 이 패치의 프롬프트 교체만으로는 부족하고, **use_input_language 를 True 로
  고정**하는 것이 근본 수정이다(그래서 아래 ④ 를 추가).
  `_replace_english_rule()` 은 그 설정이 어떤 이유로 False 가 되어도 안전하게
  한국어를 유지하도록 하는 이중 안전장치로만 남긴다.
"""
from __future__ import annotations

import os

patched: dict = {"prompt_patched": False, "max_tokens": None, "timeout": None,
                 "original_keep": False}


def _replace_english_rule(prompt: str) -> str:
    """`## Language Requirement` 블록만 한국어 유지 규칙으로 교체한다."""
    marker = "## Language Requirement"
    idx = prompt.find(marker)
    if idx < 0:
        return prompt
    out_idx = prompt.find("# Output:", idx)
    rule = (
        "## Language Requirement\n"
        "Write each memory in the SAME language the utterance was spoken in.\n"
        "Korean input stays Korean (한국어 그대로). Never translate. Keep proper "
        "nouns as spoken, and keep relative time words (지난주, 내일, 아침 9시) as spoken.\n\n"
    )
    return prompt[:idx] + rule + (prompt[out_idx:] if out_idx >= 0 else "")


def install() -> dict:
    try:
        from voicemem.leftbrain import extract_facts_openai as ef
    except Exception as exc:  # noqa: BLE001
        patched["error"] = f"import 실패: {exc}"
        return patched

    # ① 유저 프롬프트 언어 규칙
    if not getattr(ef.generate_additive_extraction_prompt, "_vm_ko", False):
        orig_gen = ef.generate_additive_extraction_prompt

        def generate_additive_extraction_prompt(*a, **kw):  # noqa: ANN002, ANN003
            return _replace_english_rule(orig_gen(*a, **kw))

        generate_additive_extraction_prompt._vm_ko = True  # type: ignore[attr-defined]
        ef.generate_additive_extraction_prompt = generate_additive_extraction_prompt
        patched["prompt_patched"] = True

    # ② max_tokens / timeout 상한 + **base_url 고정**
    #    ConflictResolver(VoiceMem=오케스트레이터 쪽)는 감싼 클래스가 아니라
    #    extract_facts_openai 모듈 전역 `OpenAI` 를 그대로 부른다. 그래서 이 패치가
    #    꺼둔 상태나 다른 클래스에서는 api_key 만 넘고 base_url 이 없어
    #    실제 OpenAI 서버로 가 버린다(실측 401 `Incorrect API key provided: ollama`).
    #    모듈 전역을 한 번 감싸 **모든** 호출에 api_key/base_url 을 채운다.
    mt = int(os.environ.get("VM_EXTRACT_MAX_TOKENS", "1024"))
    to = float(os.environ.get("VM_EXTRACT_TIMEOUT", "300"))
    base_url = os.environ.get("OPENAI_BASE_URL")
    patched["max_tokens"], patched["timeout"] = mt, to
    patched["base_url"] = base_url
    cls = ef.OpenAIMem0V3AdditiveExtractor
    # 모듈 전역 `OpenAI` 는 원래 없다(각 함수 안에서 `from openai import OpenAI`).
    # 그래서 여기에 우리 클라이언트를 **미리 심어 둔다** — extract()/ConflictResolver
    # 양쪽 모두 함수 안 import 가 이 전역을 먼저 찾게 된다.
    if not getattr(getattr(ef, "OpenAI", None), "_vm_ko", False):
        import openai as _openai_mod
        _RealClient = _openai_mod.OpenAI

        class _GlobalClient(_RealClient):  # type: ignore[misc, valid-type]
            """base_url 이 빠진 호출에 로컬 엔드포인트를 채워 넣는다."""

            def __init__(self, **ckw):  # noqa: ANN003
                if base_url and not ckw.get("base_url"):
                    ckw["base_url"] = base_url
                super().__init__(**ckw)

        _GlobalClient._vm_ko = True  # type: ignore[attr-defined]
        ef.OpenAI = _GlobalClient  # type: ignore[attr-defined]

    if not getattr(cls, "_vm_ko", False):
        orig_init = cls.__init__

        def __init__(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
            orig_init(self, *a, **kw)
            self._vm_ko_mt, self._vm_ko_to = mt, to

        def extract(self, **kw):  # noqa: ANN001
            import openai as _openai_mod
            from openai import OpenAI
            real = OpenAI

            class _Client(real):  # type: ignore[misc, valid-type]
                def __init__(self, **ckw):  # noqa: ANN003
                    ckw["timeout"] = max(float(ckw.get("timeout") or 0), to)
                    super().__init__(**ckw)

                @property
                def chat(self):  # noqa: ANN201
                    chat = super().chat
                    create = chat.completions.create

                    def _create(**ckw2):  # noqa: ANN003
                        ckw2.setdefault("max_tokens", mt)
                        return create(**ckw2)

                    chat.completions.create = _create
                    return chat

            # extract() 본문이 함수 안에서 `from openai import OpenAI` 를 다시 하므로,
            # 모듈 속성과 sys.modules 양쪽을 함께 바꿔야 실제로 _Client 가 쓰인다.
            ef.OpenAI = _Client  # type: ignore[attr-defined]
            _openai_mod.OpenAI = _Client
            try:
                return _orig_extract(self, **kw)
            finally:
                ef.OpenAI = real  # type: ignore[attr-defined]
                _openai_mod.OpenAI = real

        _orig_extract = cls.extract
        cls.__init__ = __init__
        cls.extract = extract
        cls._vm_ko = True  # type: ignore[attr-defined]

    # ③ (옵션) 원문 유지
    if os.environ.get("VM_KO_REPLACE_WITH_ORIGINAL", "0") == "1":
        if not getattr(ef.parse_additive_memory_response, "_vm_ko", False):
            import difflib
            orig_parse = ef.parse_additive_memory_response

            def parse_additive_memory_response(raw_json: str):  # noqa: ANN001
                facts = orig_parse(raw_json)
                orig = (ef._VM_KO_LAST_UTTERANCE.get("text") or "").strip()  # noqa: SLF001
                if not orig:
                    return facts
                for f in facts:
                    t = (f.text or "").strip()
                    if not t or t in orig:
                        continue
                    if difflib.SequenceMatcher(None, orig, t).ratio() >= 0.9:
                        f.text = orig
                return facts

            orig_extract_2 = cls.extract

            def extract_keep(self, **kw):  # noqa: ANN001
                msgs = kw.get("new_messages") or []
                ef._VM_KO_LAST_UTTERANCE["text"] = "\n".join(  # noqa: SLF001
                    str(m.get("content") or "") for m in msgs
                    if (m.get("role") or "user") != "assistant").strip()
                return orig_extract_2(self, **kw)

            parse_additive_memory_response._vm_ko = True  # type: ignore[attr-defined]
            ef.parse_additive_memory_response = parse_additive_memory_response
            cls.extract = extract_keep
            patched["original_keep"] = True

    # ④ use_input_language 를 True 로 고정 (근본 수정)
    #    extract() 가 client.chat.completions.create(...) 를 직접 부르면서 언어 규칙은
    #    **유저 메시지 생성 시점**에 결정되므로, 생성 함수를 감싸 config 를 강제한다.
    #    설정이 False 인 경로(오케스트레이터가 만드는 추출기 등)까지 덮는다.
    if not getattr(cls.__init__, "_vm_ko_lang", False):
        _orig_init2 = cls.__init__

        def __init_lang(self, config=None, *a, **kw):  # noqa: ANN001
            if config is None:
                config = ef.OpenAIAdditiveExtractorConfig()
            try:
                config.use_input_language = True
            except Exception:  # noqa: BLE001  (dataclass 아닐 수도 있음)
                pass
            _orig_init2(self, config, *a, **kw)

        __init_lang._vm_ko_lang = True  # type: ignore[attr-defined]
        cls.__init__ = __init_lang
        patched["use_input_language"] = True

    return patched


PATCHED = install()
