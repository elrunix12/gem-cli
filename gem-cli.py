#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time
from contextlib import closing, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import readline  # Habilita edição de linha, setas ↑/↓ e Ctrl+R no input()
except ImportError:
    readline = None  # type: ignore[assignment]


def _has_dependencies() -> bool:
    """Verifica se as dependências externas existem no interpretador atual."""
    try:
        return bool(
            importlib.util.find_spec("google.genai")
            and importlib.util.find_spec("rich")
        )
    except ModuleNotFoundError:
        return False


def bootstrap_venv() -> None:
    """Reexecuta o script no .venv local se as dependências não estiverem no Python atual."""
    if _has_dependencies() or os.environ.get("_GEMINI_CLI_BOOTSTRAPPED") == "1":
        return

    xdg_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    share_venv = xdg_data / "gem-cli" / ".venv" / "bin" / "python"
    script_venv = Path(__file__).resolve().parent / ".venv" / "bin" / "python"

    for candidate in (share_venv, script_venv):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            try:
                if os.path.samefile(sys.executable, candidate):
                    return
            except OSError:
                pass
            os.environ["_GEMINI_CLI_BOOTSTRAPPED"] = "1"
            script_path = str(Path(__file__).resolve())
            os.execv(str(candidate), [str(candidate), script_path, *sys.argv[1:]])


bootstrap_venv()

try:
    from google import genai
    from google.genai import errors, types
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.padding import Padding
    from rich.spinner import Spinner
except ImportError as exc:
    print(
        f"[!] Dependência ausente: {exc.name or exc}. "
        "Instale com: pip install google-genai rich",
        file=sys.stderr,
    )
    sys.exit(1)

DEFAULT_MODEL = "gemini-3.5-flash-lite"
SHORT_INSTRUCTION = (
    "Responda de forma curta, direta e objetiva, ideal para leitura no terminal. "
    "Vá direto ao ponto, sem introduções longas ou explicações extras a menos que pedido."
)
DEFAULT_COLORS = {
    "COLOR_USER": "137;180;250",
    "COLOR_GEMINI": "203;166;247",
    "COLOR_SYSTEM": "166;173;200",
    "COLOR_SUCCESS": "166;227;161",
    "COLOR_ERROR": "243;139;168",
}

RETRYABLE_SSE_CODES = {
    "service_unavailable",
    "unavailable",
    "overloaded",
    "internal_error",
    "deadline_exceeded",
    "gateway_timeout",
    "too_many_requests",
    "resource_exhausted",
    "rate_limit_exceeded",
}
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}
RETRYABLE_NETWORK_EXCEPTIONS = {
    "APIConnectionError",
    "APITimeoutError",
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectError",
    "RemoteProtocolError",
    "ReadError",
}
TERMINAL_FAILURE_STATUSES = {"failed", "cancelled"}
TRUNCATED_STATUSES = {"incomplete", "requires_continuation", "budget_exceeded"}
QUOTA_RE = re.compile(r"\b(quota|billing|plan|daily limit)\b|requests-per-day", re.IGNORECASE)


class TransientStreamError(RuntimeError):
    """Erro transitório recebido via evento SSE durante o streaming."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if code else message)


class InteractionFailedError(RuntimeError):
    """Erro terminal quando a interação falha ou é cancelada no servidor."""


@dataclass
class Config:
    config_dir: Path
    env_file: Path
    prompt_dir: Path
    save_dir: Path
    api_key: str = ""
    model: str = DEFAULT_MODEL
    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))


def get_field(obj: Any, name: str, default: Any = None) -> Any:
    """Lê atributo de objeto ou chave de dicionário de forma uniforme."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def get_http_status_code(exc: Exception) -> int | None:
    """Extrai código HTTP tanto de errors.APIError (.code) quanto de interactions (.status_code)."""
    for attr in ("status_code", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    return None


def get_error_message(exc: Exception) -> str:
    """Extrai a mensagem legível de erros da API Gemini/Interactions."""
    msg = getattr(exc, "message", None)
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    return str(exc) or type(exc).__name__


def normalize_error_code(code: str) -> str:
    """Normaliza códigos SSE curtos ou em formato URI para o identificador final."""
    cleaned = code.strip().lower().rstrip("/")
    if "/" in cleaned:
        return cleaned.rsplit("/", 1)[-1]
    return cleaned


def is_quota_exhausted_message(message: str) -> bool:
    """Detecta erros 429 de cota/faturamento que não se resolvem com retry imediato."""
    return bool(QUOTA_RE.search(message))


def is_retryable_exception(exc: Exception) -> bool:
    """Determina se a exceção justifica nova tentativa automática."""
    if isinstance(exc, InteractionFailedError):
        return False
    status_code = get_http_status_code(exc)
    if status_code == 429 and is_quota_exhausted_message(get_error_message(exc)):
        return False
    if status_code in RETRYABLE_HTTP_CODES:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return type(exc).__name__ in RETRYABLE_NETWORK_EXCEPTIONS


def get_render_interval(text_len: int) -> float:
    """Ajusta o intervalo de re-renderização do Markdown conforme o tamanho da resposta."""
    if text_len < 4_096:
        return 1.0 / 12.0
    if text_len < 16_384:
        return 1.0 / 5.0
    return 0.5


def parse_rgb(rgb_str: str, fallback: str) -> tuple[str, str, str]:
    """Valida string 'R;G;B' e retorna tupla ('R', 'G', 'B')."""
    parts = [p.strip() for p in rgb_str.split(";")]
    if len(parts) == 3 and all(p.isascii() and p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return parts[0], parts[1], parts[2]
    fb = fallback.split(";")
    return fb[0], fb[1], fb[2]


def to_rich_color(rgb_str: str, fallback: str) -> str:
    r, g, b = parse_rgb(rgb_str, fallback)
    return f"rgb({r},{g},{b})"


def to_readline_prompt(text: str, rgb_str: str, fallback: str) -> str:
    """Formata prompt ANSI com \\001...\\002 somente quando GNU Readline em TTY estiver ativo."""
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    r, g, b = parse_rgb(rgb_str, fallback)
    ansi_start = f"\033[38;2;{r};{g};{b}m"
    ansi_end = "\033[0m"
    use_rl_markers = (
        readline is not None
        and sys.stdin.isatty()
        and "libedit" not in (getattr(readline, "__doc__", "") or "").lower()
    )
    if use_rl_markers:
        return f"\001{ansi_start}\002{text}\001{ansi_end}\002"
    return f"{ansi_start}{text}{ansi_end}"


def _parse_env_value(raw_value: str) -> str:
    """Remove aspas externas e comentários inline (# precedido de espaço fora de aspas)."""
    value = raw_value.strip()
    if value.startswith(('"', "'")):
        quote = value[0]
        end_idx = value.find(quote, 1)
        if end_idx != -1:
            trailing = value[end_idx + 1 :].strip()
            if not trailing or trailing.startswith("#"):
                return value[1:end_idx]
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def load_config(model_override: str | None = None) -> Config:
    """Carrega configuração com precedência: CLI > ambiente > arquivo env > padrões."""
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    config_dir = xdg_config / "python" / "gem-cli"
    cfg = Config(
        config_dir=config_dir,
        env_file=config_dir / "env",
        prompt_dir=config_dir / "prompts",
        save_dir=Path.home() / "Documentos" / "Gemini",
    )

    if cfg.env_file.is_file():
        try:
            env_content = cfg.env_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f'Não foi possível ler "{cfg.env_file}": {exc}') from exc

        for raw_line in env_content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, raw_value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            value = _parse_env_value(raw_value)

            if key in ("API_KEY", "GEMINI_API_KEY") and value:
                cfg.api_key = value
            elif key in ("MODEL", "GEMINI_MODEL") and value:
                cfg.model = value
            elif key == "SAVE_DIR" and value:
                cfg.save_dir = Path(value).expanduser()
            elif key == "PROMPT_DIR" and value:
                cfg.prompt_dir = Path(value).expanduser()
            elif key in cfg.colors and value:
                cfg.colors[key] = value

    # Variáveis de ambiente têm precedência sobre o arquivo env
    env_api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("API_KEY")
    if env_api_key:
        cfg.api_key = env_api_key

    env_model = os.environ.get("GEMINI_MODEL") or os.environ.get("MODEL")
    if env_model:
        cfg.model = env_model

    env_save_dir = os.environ.get("GEMINI_SAVE_DIR") or os.environ.get("SAVE_DIR")
    if env_save_dir:
        cfg.save_dir = Path(env_save_dir).expanduser()

    env_prompt_dir = os.environ.get("GEMINI_PROMPT_DIR") or os.environ.get("PROMPT_DIR")
    if env_prompt_dir:
        cfg.prompt_dir = Path(env_prompt_dir).expanduser()

    for color_key in list(cfg.colors):
        env_color = os.environ.get(color_key)
        if env_color:
            cfg.colors[color_key] = env_color

    if model_override:
        cfg.model = model_override

    if not cfg.api_key:
        if not cfg.env_file.is_file():
            raise FileNotFoundError(
                f'Arquivo de configuração não encontrado em "{cfg.env_file}" '
                'e variável "GEMINI_API_KEY" não definida.'
            )
        raise ValueError(f'"API_KEY" / "GEMINI_API_KEY" não encontrada em {cfg.env_file}.')

    return cfg


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Copia texto para a área de transferência no Wayland, X11 ou macOS."""
    candidates: list[list[str]] = []
    if os.environ.get("WAYLAND_DISPLAY"):
        candidates.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        candidates.extend([
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ])
    if sys.platform == "darwin":
        candidates.append(["pbcopy"])

    # Fallback caso variáveis de sessão não estejam exportadas
    for fallback_cmd in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["pbcopy"],
    ):
        if fallback_cmd not in candidates:
            candidates.append(fallback_cmd)

    tried_errors: list[str] = []
    for cmd in candidates:
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=5,
            )
            return True, ""
        except (subprocess.SubprocessError, OSError) as exc:
            tried_errors.append(f"{cmd[0]}: {exc}")

    if tried_errors:
        return False, "Falha ao copiar (" + "; ".join(tried_errors) + ")."
    return False, "Nenhum utilitário de clipboard encontrado (wl-copy, xclip, xsel ou pbcopy)."


def extract_code_blocks(markdown_text: str) -> str:
    """Extrai blocos de código cercados (``` ou ~~~) seguindo as regras do CommonMark."""
    blocks: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    current: list[str] = []

    for line in markdown_text.splitlines():
        stripped = line.lstrip(" \t")
        if not in_fence:
            match = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
            if match:
                marker, info = match.group(1), match.group(2)
                if marker[0] == "`" and "`" in info:
                    continue
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
                current = []
        else:
            close_match = re.match(
                rf"^{re.escape(fence_char)}{{{fence_len},}}[ \t]*$",
                stripped,
            )
            if close_match:
                content = "\n".join(current).strip("\r\n")
                if content.strip():
                    blocks.append(textwrap.dedent(content))
                in_fence = False
                current = []
            else:
                current.append(line)

    return "\n\n".join(blocks)


def extract_text_from_step(step: Any) -> str:
    """Extrai texto de um step do tipo model_output (schema GA)."""
    if get_field(step, "type") != "model_output":
        return ""
    content = get_field(step, "content") or []
    chunks: list[str] = []
    for block in content:
        block_type = get_field(block, "type")
        if block_type in ("text", None):
            txt = get_field(block, "text")
            if txt:
                chunks.append(txt)
    return "".join(chunks)


def extract_outputs_fallback(interaction: Any) -> str:
    """Extrai texto da interação (compatível com output_text, steps e legado outputs)."""
    if interaction is None:
        return ""

    # 1. Atalho oficial do SDK (schema GA)
    output_text = get_field(interaction, "output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    # 2. Schema GA: interaction.steps -> model_output -> content
    steps = get_field(interaction, "steps") or []
    chunks: list[str] = []
    for step in steps:
        txt = extract_text_from_step(step)
        if txt:
            chunks.append(txt)
    if chunks:
        return "".join(chunks)

    # 3. Compatibilidade com schema legado: interaction.outputs
    outputs = get_field(interaction, "outputs") or []
    for item in outputs:
        if get_field(item, "type") == "text":
            txt = get_field(item, "text")
            if txt:
                chunks.append(txt)
    return "".join(chunks)


class GeminiCLI:
    def __init__(
        self,
        cfg: Config,
        prompt_arg: str | None = None,
        short_mode: bool | None = None,
        debug: bool = False,
    ) -> None:
        self.cfg = cfg
        self.debug = debug
        self.console = Console()
        self.c_gemini = to_rich_color(cfg.colors["COLOR_GEMINI"], DEFAULT_COLORS["COLOR_GEMINI"])
        self.c_sys = to_rich_color(cfg.colors["COLOR_SYSTEM"], DEFAULT_COLORS["COLOR_SYSTEM"])
        self.c_succ = to_rich_color(cfg.colors["COLOR_SUCCESS"], DEFAULT_COLORS["COLOR_SUCCESS"])
        self.c_err = to_rich_color(cfg.colors["COLOR_ERROR"], DEFAULT_COLORS["COLOR_ERROR"])

        self.prompt_user = to_readline_prompt(
            "Você: ", cfg.colors["COLOR_USER"], DEFAULT_COLORS["COLOR_USER"]
        )
        self.prompt_select = to_readline_prompt(
            "Arquivo de prompt (ou Enter para nenhum): ",
            cfg.colors["COLOR_SYSTEM"],
            DEFAULT_COLORS["COLOR_SYSTEM"],
        )
        self.prompt_short = to_readline_prompt(
            "Respostas curtas? [S/n]: ",
            cfg.colors["COLOR_SYSTEM"],
            DEFAULT_COLORS["COLOR_SYSTEM"],
        )
        self.prompt_save = to_readline_prompt(
            "  ↳ Digite o nome do arquivo (sem .md): ",
            cfg.colors["COLOR_SYSTEM"],
            DEFAULT_COLORS["COLOR_SYSTEM"],
        )
        self.prompt_overwrite = to_readline_prompt(
            "  ↳ Arquivo já existe. Sobrescrever? [s/N]: ",
            cfg.colors["COLOR_SYSTEM"],
            DEFAULT_COLORS["COLOR_SYSTEM"],
        )

        self.system_instruction: str | None = None
        self.prompt_file_name: str = ""
        self.short_mode: bool = True if short_mode is None else short_mode
        self.previous_interaction_id: str = ""
        self.last_response: str = ""
        self.history: list[tuple[str, str]] = []

        if prompt_arg:
            self._load_prompt_file(prompt_arg)

        # Retries são controlados manualmente em send_message() com feedback visual no spinner.
        # Requer google-genai atualizado para que attempts=1 desative retries internos em Interactions.
        http_options = types.HttpOptions(
            timeout=180_000,
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        self.client = genai.Client(api_key=cfg.api_key, http_options=http_options)

    def _print(self, text: str, style: str | None = None, *, end: str = "\n") -> None:
        """Imprime texto literal com estilo Rich, sem risco de MarkupError."""
        self.console.print(text, style=style, markup=False, end=end)

    def _load_prompt_file(self, choice: str) -> None:
        safe_name = Path(choice).name
        if not safe_name or safe_name in (".", ".."):
            self._print(f'[-] Aviso: Nome de prompt inválido: "{choice}".', self.c_err)
            return

        candidate = self.cfg.prompt_dir / safe_name
        if candidate.is_file():
            try:
                content = candidate.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                self._print(f'[-] Erro ao ler "{candidate}": {exc}', self.c_err)
                return
            self.system_instruction = content or None
            self.prompt_file_name = safe_name
            self._print(f'[+] Prompt "{safe_name}" carregado!', self.c_succ)
        else:
            self._print(f'[-] Aviso: Arquivo "{candidate}" não encontrado.', self.c_err)

    def select_prompt_interactive(self) -> bool:
        sep = "=" * 50
        self._print(sep, self.c_sys)

        if self.cfg.prompt_dir.is_dir():
            try:
                available = sorted(p.name for p in self.cfg.prompt_dir.iterdir() if p.is_file())
            except OSError as exc:
                available = []
                self._print(f"[-] Não foi possível listar prompts: {exc}", self.c_err)
            if available:
                self._print(f"Prompts disponíveis em {self.cfg.prompt_dir}: ", self.c_sys, end="")
                self._print(" ".join(available))

        try:
            choice = input(self.prompt_select).strip()
        except (EOFError, KeyboardInterrupt):
            self._print("\nEncerrando...", self.c_sys)
            return False

        if choice:
            self._load_prompt_file(choice)
        return True

    def select_short_mode_interactive(self) -> bool:
        try:
            choice = input(self.prompt_short).strip().lower()
        except (EOFError, KeyboardInterrupt):
            self._print("\nEncerrando...", self.c_sys)
            return False

        if choice in ("n", "nao", "não", "no", "0", "off"):
            self.short_mode = False
            self._print("[*] Modo respostas curtas: DESATIVADO", self.c_sys)
        else:
            self.short_mode = True
            self._print("[+] Modo respostas curtas: ATIVADO", self.c_succ)
        return True

    def print_banner(self) -> None:
        sep = "=" * 50
        modo = "Curto" if self.short_mode else "Detalhado"
        self._print(sep, self.c_sys)
        self._print(f"🤖 Gemini Terminal [{self.cfg.model}] ({modo})", self.c_succ)
        self._print(
            "   Comandos: /copy | /code | /save | /curto | /reset | !<bash> | sair",
            self.c_sys,
        )
        self._print(sep, self.c_sys)

    def handle_save(self) -> None:
        if not self.history:
            self._print("  ↳ O histórico está vazio. Nada para salvar.", self.c_err)
            return

        try:
            raw_name = input(self.prompt_save).strip()
        except (EOFError, KeyboardInterrupt):
            self._print("\n  ↳ Operação cancelada.", self.c_err)
            return

        safe_name = Path(raw_name).name
        if not safe_name or safe_name in (".", "..", ".md"):
            self._print("  ↳ Nome de arquivo inválido. Operação cancelada.", self.c_err)
            return

        if not safe_name.endswith(".md"):
            safe_name = f"{safe_name}.md"

        try:
            self.cfg.save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._print(f'  ↳ Erro ao criar diretório "{self.cfg.save_dir}": {exc}', self.c_err)
            return

        filepath = self.cfg.save_dir / safe_name

        if filepath.exists():
            try:
                confirm = input(self.prompt_overwrite).strip().lower()
            except (EOFError, KeyboardInterrupt):
                self._print("\n  ↳ Operação cancelada.", self.c_err)
                return
            if confirm not in ("s", "sim", "y", "yes"):
                self._print("  ↳ Operação cancelada.", self.c_err)
                return

        lines = [f"# Histórico - Gemini Terminal ({self.cfg.model})\n"]
        if self.prompt_file_name:
            lines.append(f"**Prompt Carregado:** `{self.prompt_file_name}`\n\n---\n")

        for user_msg, model_msg in self.history:
            lines.append(f"### Você:\n{user_msg}\n\n### Gemini:\n{model_msg}\n\n---\n")

        try:
            filepath.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            self._print(f'  ↳ Erro ao salvar arquivo "{filepath}": {exc}', self.c_err)
            return

        self._print(f"  ↳ Sucesso! Arquivo salvo em: {filepath}", self.c_succ)

    def handle_shell(self, bash_cmd: str) -> None:
        if not bash_cmd:
            return
        shell = shutil.which("bash") or "/bin/sh"
        try:
            subprocess.run(bash_cmd, shell=True, executable=shell)
        except KeyboardInterrupt:
            print()
        except OSError as exc:
            self._print(f"  ↳ Erro ao executar shell: {exc}", self.c_err)

    def _print_partial_if_any(self, text: str) -> None:
        if text.strip():
            self.console.print(Padding(Markdown(text), (0, 3, 0, 0)))

    def _stream_context(self, raw_stream: Any) -> Any:
        if hasattr(raw_stream, "__enter__"):
            return raw_stream
        if hasattr(raw_stream, "close"):
            return closing(raw_stream)
        return nullcontext(raw_stream)

    def send_message(self, user_input: str) -> None:
        request_kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "input": user_input,
            "stream": True,
        }
        if self.previous_interaction_id:
            request_kwargs["previous_interaction_id"] = self.previous_interaction_id

        instructions: list[str] = []
        if self.system_instruction:
            instructions.append(self.system_instruction)
        if self.short_mode:
            instructions.append(SHORT_INSTRUCTION)
        if instructions:
            request_kwargs["system_instruction"] = "\n\n".join(instructions)

        full_text = ""
        last_partial_text = ""
        interaction_id = ""
        final_status = ""
        max_attempts = 3

        if self.debug:
            self._print(
                f"[debug] model={self.cfg.model!r} "
                f"previous_id={self.previous_interaction_id!r} "
                f"short_mode={self.short_mode!r}",
                self.c_sys,
            )

        self._print("\nGemini:", self.c_gemini)

        try:
            spinner = Spinner("dots", text=" Conectando...", style=self.c_sys)
            with Live(
                spinner,
                console=self.console,
                refresh_per_second=12,
                transient=True,
                vertical_overflow="ellipsis",
            ) as live:
                for attempt in range(1, max_attempts + 1):
                    full_text = ""
                    interaction_id = ""
                    final_status = ""
                    step_types: dict[int, str] = {}
                    last_render_at = 0.0

                    def update_markdown(force: bool = False) -> None:
                        nonlocal last_render_at
                        now = time.monotonic()
                        interval = get_render_interval(len(full_text))
                        if force or (now - last_render_at) >= interval:
                            live.update(Padding(Markdown(full_text), (0, 3, 0, 0)))
                            last_render_at = now

                    if attempt > 1:
                        live.update(
                            Spinner(
                                "dots",
                                text=f" Tentando novamente ({attempt}/{max_attempts})...",
                                style=self.c_sys,
                            )
                        )

                    try:
                        raw_stream = self.client.interactions.create(**request_kwargs)
                        with self._stream_context(raw_stream) as stream:
                            for event in stream:
                                event_type = get_field(event, "event_type") or get_field(event, "type")

                                if self.debug:
                                    live.console.print(
                                        f"[debug] event={event_type!r} -> {event!r}",
                                        style=self.c_sys,
                                        markup=False,
                                    )

                                if event_type in ("interaction.start", "interaction.created"):
                                    step_types.clear()
                                    interaction = get_field(event, "interaction")
                                    new_id = get_field(interaction, "id")
                                    if new_id:
                                        interaction_id = new_id
                                    if not full_text:
                                        live.update(
                                            Spinner("dots", text=" Pensando...", style=self.c_sys)
                                        )

                                elif event_type == "interaction.status_update":
                                    upd_id = get_field(event, "interaction_id")
                                    if upd_id and not interaction_id:
                                        interaction_id = upd_id
                                    status = str(get_field(event, "status") or "")
                                    if status:
                                        final_status = status
                                    if status in TERMINAL_FAILURE_STATUSES:
                                        raise InteractionFailedError(
                                            f"A interação terminou com status '{status}'."
                                        )
                                    if status and not full_text:
                                        live.update(
                                            Spinner(
                                                "dots",
                                                text=f" Status: {status}...",
                                                style=self.c_sys,
                                            )
                                        )

                                elif event_type == "step.start":
                                    step = get_field(event, "step")
                                    step_type = str(get_field(step, "type") or "")
                                    step_index = get_field(event, "index")
                                    if isinstance(step_index, int) and step_type:
                                        step_types[step_index] = step_type

                                    if step_type == "thought":
                                        if not full_text:
                                            live.update(
                                                Spinner(
                                                    "dots",
                                                    text=" Raciocinando...",
                                                    style=self.c_sys,
                                                )
                                            )
                                    elif step_type == "model_output":
                                        initial_chunk = extract_text_from_step(step)
                                        if initial_chunk:
                                            full_text += initial_chunk
                                            update_markdown()

                                elif event_type in ("content.delta", "step.delta"):
                                    delta = get_field(event, "delta")
                                    delta_type = get_field(delta, "type")
                                    step_index = get_field(event, "index")
                                    current_step_type = (
                                        step_types.get(step_index)
                                        if isinstance(step_index, int)
                                        else None
                                    )

                                    if delta_type in (
                                        "thought",
                                        "thought_signature",
                                        "thought_summary",
                                    ):
                                        if not full_text:
                                            live.update(
                                                Spinner(
                                                    "dots",
                                                    text=" Raciocinando...",
                                                    style=self.c_sys,
                                                )
                                            )
                                    elif delta_type in ("text", None) and current_step_type in (
                                        None,
                                        "model_output",
                                    ):
                                        chunk = get_field(delta, "text")
                                        if chunk:
                                            full_text += chunk
                                            update_markdown()

                                elif event_type in ("step.stop", "done"):
                                    if full_text:
                                        update_markdown(force=True)

                                elif event_type in ("interaction.complete", "interaction.completed"):
                                    interaction = get_field(event, "interaction")
                                    new_id = get_field(interaction, "id")
                                    if new_id:
                                        interaction_id = new_id
                                    status = str(get_field(interaction, "status") or "")
                                    if status:
                                        final_status = status
                                    if status in TERMINAL_FAILURE_STATUSES:
                                        raise InteractionFailedError(
                                            f"A interação terminou com status '{status}'."
                                        )
                                    if not full_text.strip():
                                        full_text = extract_outputs_fallback(interaction)
                                    if full_text.strip():
                                        update_markdown(force=True)

                                elif event_type in ("error", "interaction.error"):
                                    err_obj = get_field(event, "error")
                                    raw_code = str(get_field(err_obj, "code") or "")
                                    err_code = normalize_error_code(raw_code)
                                    err_msg = str(
                                        get_field(err_obj, "message")
                                        or err_obj
                                        or "Erro durante a geração."
                                    )
                                    is_retryable_sse = err_code in RETRYABLE_SSE_CODES and not (
                                        err_code in ("too_many_requests", "resource_exhausted")
                                        and is_quota_exhausted_message(err_msg)
                                    )
                                    if is_retryable_sse:
                                        raise TransientStreamError(err_code, err_msg)
                                    raise InteractionFailedError(
                                        f"[{err_code}] {err_msg}" if err_code else err_msg
                                    )

                                else:
                                    direct_text = get_field(event, "text")
                                    if direct_text:
                                        full_text += direct_text
                                        update_markdown()

                        # Fallback final: se o stream encerrou sem texto mas temos interaction_id
                        if (
                            not full_text.strip()
                            and interaction_id
                            and final_status not in TERMINAL_FAILURE_STATUSES
                        ):
                            live.update(
                                Spinner("dots", text=" Recuperando resposta...", style=self.c_sys)
                            )
                            try:
                                fetched = self.client.interactions.get(id=interaction_id)
                                fetched_status = str(get_field(fetched, "status") or "")
                                if fetched_status:
                                    final_status = fetched_status
                                if final_status in TERMINAL_FAILURE_STATUSES:
                                    raise InteractionFailedError(
                                        f"A interação terminou com status '{final_status}'."
                                    )
                                full_text = extract_outputs_fallback(fetched)
                            except InteractionFailedError:
                                raise
                            except Exception as fetch_err:
                                if self.debug:
                                    live.console.print(
                                        f"[debug] fallback get() falhou: {fetch_err}",
                                        style=self.c_sys,
                                        markup=False,
                                    )

                        # Tentativa concluída sem exceção
                        break

                    except TransientStreamError as err:
                        if full_text.strip():
                            last_partial_text = full_text
                        if attempt < max_attempts:
                            delay = (2 ** attempt) + random.uniform(0.0, 0.5)
                            live.update(
                                Spinner(
                                    "dots",
                                    text=(
                                        f" Servidor ocupado ({err.code}). "
                                        f"Nova tentativa em {delay:.1f}s "
                                        f"({attempt + 1}/{max_attempts})..."
                                    ),
                                    style=self.c_sys,
                                )
                            )
                            time.sleep(delay)
                            continue
                        raise

                    except Exception as err:
                        if full_text.strip():
                            last_partial_text = full_text
                        if is_retryable_exception(err) and attempt < max_attempts:
                            delay = (2 ** attempt) + random.uniform(0.0, 0.5)
                            label = get_http_status_code(err) or type(err).__name__
                            live.update(
                                Spinner(
                                    "dots",
                                    text=(
                                        f" Servidor indisponível ({label}). "
                                        f"Nova tentativa em {delay:.1f}s "
                                        f"({attempt + 1}/{max_attempts})..."
                                    ),
                                    style=self.c_sys,
                                )
                            )
                            time.sleep(delay)
                            continue
                        raise

            # Imprime o Markdown final completo fora do Live (evita cortes verticais do terminal)
            if full_text.strip():
                self.console.print(Padding(Markdown(full_text), (0, 3, 0, 0)))
            else:
                extra = f" (status: {final_status})" if final_status else ""
                self._print(f"  ↳ O modelo retornou uma resposta vazia{extra}.", self.c_err)
                return

            if final_status in TRUNCATED_STATUSES:
                self._print(
                    f"\n  ↳ [Aviso] Resposta possivelmente incompleta (status: {final_status}).",
                    self.c_err,
                )

            self.last_response = full_text
            if interaction_id and final_status not in TERMINAL_FAILURE_STATUSES:
                self.previous_interaction_id = interaction_id
            self.history.append((user_input, full_text))

        except KeyboardInterrupt:
            self._print_partial_if_any(full_text or last_partial_text)
            self._print("\n  ↳ [Aviso] Requisição cancelada pelo usuário.", self.c_err)

        except TransientStreamError as e:
            partial = full_text or last_partial_text
            if partial.strip():
                self._print_partial_if_any(partial)
                self._print(
                    f"\n  ↳ [Resposta interrompida - {e.code}]: {e.message}",
                    self.c_err,
                )
            else:
                self._print(
                    f"\n  ↳ [Servidor indisponível ({e.code})]: {e.message}",
                    self.c_err,
                )
            self._print(
                f"    Dica: o modelo '{self.cfg.model}' está com pico de demanda. "
                "Tente novamente em instantes ou use --model para alternar.",
                self.c_sys,
            )

        except InteractionFailedError as e:
            self._print_partial_if_any(full_text or last_partial_text)
            self._print(f"\n  ↳ [Erro da Interação]: {e}", self.c_err)

        except Exception as e:
            self._print_partial_if_any(full_text or last_partial_text)
            status_code = get_http_status_code(e)
            err_msg = get_error_message(e)

            if status_code == 503:
                self._print(
                    f"\n  ↳ [Servidor sobrecarregado (503)]: "
                    f"o modelo '{self.cfg.model}' está com alta demanda no Google agora.",
                    self.c_err,
                )
            elif status_code == 404:
                self._print(
                    f"\n  ↳ [Modelo ou recurso não encontrado (404)]: {err_msg}",
                    self.c_err,
                )
                self._print(
                    f"    Dica: verifique o modelo '{self.cfg.model}' no {self.cfg.env_file}.",
                    self.c_sys,
                )
            elif status_code == 429:
                self._print(f"\n  ↳ [Limite/cota excedido (429)]: {err_msg}", self.c_err)
            elif status_code in (401, 403):
                self._print(
                    f"\n  ↳ [Erro de autenticação/permissão ({status_code})]: {err_msg}",
                    self.c_err,
                )
            elif status_code is not None or isinstance(e, errors.APIError):
                code_label = status_code if status_code is not None else "API"
                self._print(
                    f"\n  ↳ [Erro da API Gemini ({code_label})]: {err_msg}",
                    self.c_err,
                )
            else:
                self._print(
                    f"\n  ↳ [Erro de Requisição]: {type(e).__name__}: {err_msg}",
                    self.c_err,
                )

            if status_code in (400, 404) and self.previous_interaction_id:
                self._print(
                    "    Dica: se o contexto da conversa expirou no servidor, use /reset para iniciar uma nova sessão.",
                    self.c_sys,
                )

    def run(
        self,
        skip_prompt_select: bool = False,
        skip_short_select: bool = False,
    ) -> int:
        try:
            if not skip_prompt_select and not self.select_prompt_interactive():
                return 0

            if not skip_short_select and not self.select_short_mode_interactive():
                return 0

            self.print_banner()

            while True:
                print()
                try:
                    user_input = input(self.prompt_user)
                except (EOFError, KeyboardInterrupt):
                    self._print("\nEncerrando...", self.c_sys)
                    break

                trimmed = user_input.strip()
                if not trimmed:
                    continue

                cmd_lower = trimmed.lower()

                if cmd_lower in ("sair", "exit", "quit"):
                    self._print("Encerrando...", self.c_sys)
                    break

                if trimmed.startswith("!"):
                    self.handle_shell(trimmed[1:].strip())
                    continue

                if cmd_lower == "/curto":
                    self.short_mode = not self.short_mode
                    estado = "ATIVADO" if self.short_mode else "DESATIVADO"
                    self._print(f"  ↳ Modo respostas curtas: {estado}", self.c_succ)
                    continue

                if cmd_lower in ("/reset", "/clear"):
                    self.previous_interaction_id = ""
                    self.last_response = ""
                    self.history.clear()
                    self._print("  ↳ Conversa e contexto reiniciados!", self.c_succ)
                    continue

                if cmd_lower == "/copy":
                    if not self.last_response:
                        self._print("  ↳ Sem conteúdo para copiar.", self.c_err)
                        continue
                    ok, err = copy_to_clipboard(self.last_response)
                    if ok:
                        self._print("  ↳ Resposta copiada!", self.c_succ)
                    else:
                        self._print(f"  ↳ Erro: {err}", self.c_err)
                    continue

                if cmd_lower == "/code":
                    if not self.last_response:
                        self._print("  ↳ Sem conteúdo para copiar.", self.c_err)
                        continue
                    code_out = extract_code_blocks(self.last_response)
                    if not code_out:
                        self._print("  ↳ Nenhum bloco de código encontrado.", self.c_err)
                        continue
                    ok, err = copy_to_clipboard(code_out)
                    if ok:
                        self._print("  ↳ Blocos de código copiados!", self.c_succ)
                    else:
                        self._print(f"  ↳ Erro: {err}", self.c_err)
                    continue

                if cmd_lower == "/save":
                    self.handle_save()
                    continue

                self.send_message(trimmed)

            return 0
        finally:
            self.client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CLI interativa para Gemini Interactions API")
    parser.add_argument("--model", help="Sobrescreve o modelo definido no arquivo env")
    parser.add_argument(
        "--prompt",
        help="Nome do arquivo de system prompt em ~/.config/python/gem-cli/prompts",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--curto", action="store_true", help="Inicia com respostas curtas ativadas")
    mode_group.add_argument("--longo", action="store_true", help="Inicia com respostas detalhadas")
    parser.add_argument("--debug", action="store_true", help="Exibe os eventos brutos recebidos da API")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        cfg = load_config(model_override=args.model)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"[!] Erro: {exc}", file=sys.stderr)
        return 1

    short_mode_arg: bool | None = None
    if args.curto:
        short_mode_arg = True
    elif args.longo:
        short_mode_arg = False

    cli = GeminiCLI(cfg, prompt_arg=args.prompt, short_mode=short_mode_arg, debug=args.debug)
    return cli.run(
        skip_prompt_select=bool(args.prompt),
        skip_short_select=short_mode_arg is not None,
    )


if __name__ == "__main__":
    sys.exit(main())