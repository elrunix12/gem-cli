#!/usr/bin/env bash
# Instalador do gem-cli (projeto não oficial, sem relação com o Google).
#
# Uso:
#   curl -fsSL https://raw.githubusercontent.com/elrunix12/gem-cli/main/install.sh | bash
#
# Variável opcional:
#   GEM_CLI_DIR   pasta de instalação (padrão: ~/.local/share/gem-cli)
#
# Cria o comando "gem-cli" em ~/.local/bin (não altera .bashrc nem .zshrc).

set -euo pipefail

REPO_URL="https://github.com/elrunix12/gem-cli.git"
INSTALL_DIR="${GEM_CLI_DIR:-$HOME/.local/share/gem-cli}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/python/gem-cli"
SCRIPT_NAME="gem-cli.py"

# Garante um caminho absoluto (o comando gem-cli guarda esse caminho).
case "$INSTALL_DIR" in
  /*) ;;
  *)  INSTALL_DIR="$PWD/$INSTALL_DIR" ;;
esac

if [ -t 1 ]; then
  C_OK=$'\033[32m'
  C_WARN=$'\033[33m'
  C_ERR=$'\033[31m'
  C_OFF=$'\033[0m'
else
  C_OK=""
  C_WARN=""
  C_ERR=""
  C_OFF=""
fi

info() { printf '%s\n' "$*"; }
ok()   { printf '%s[+]%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s[*]%s %s\n' "$C_WARN" "$C_OFF" "$*"; }
die()  { printf '%s[!]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

# Com "curl | bash" a entrada padrão é o próprio script, então as perguntas
# são feitas pelo terminal (/dev/tty), quando existir.
HAS_TTY=0
if ( exec </dev/tty ) 2>/dev/null; then
  HAS_TTY=1
fi

# Pergunta sim/não pelo terminal. $1 = pergunta, $2 = padrão (S ou N).
# Retorna 0 para "sim" e 1 para "não".
ask_yn() {
  local question="$1" default="$2" answer=""
  if [ "$default" = "S" ]; then
    printf '%s [S/n]: ' "$question" >/dev/tty
  else
    printf '%s [s/N]: ' "$question" >/dev/tty
  fi
  read -r answer </dev/tty || answer=""
  case "$answer" in
    s|S|sim|Sim|SIM|y|Y|yes|Yes) return 0 ;;
    n|N|nao|não|Nao|Não|no|No)   return 1 ;;
    *) [ "$default" = "S" ] ;;
  esac
}

# --- Administrador (sudo) e gerenciador de pacotes --------------------------

SUDO_PREFIX=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO_PREFIX="sudo "
fi

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

can_root() {
  [ "$(id -u)" -eq 0 ] || command -v sudo >/dev/null 2>&1
}

# Detecta sistemas imutáveis (base somente leitura), onde instalar pacotes
# com apt/dnf/pacman não é o caminho certo.
is_immutable_system() {
  if [ -e /run/ostree-booted ] || [ -e /etc/NIXOS ]; then
    return 0
  fi
  if command -v transactional-update >/dev/null 2>&1; then
    return 0
  fi
  if [ -r /etc/os-release ]; then
    # shellcheck source=/dev/null
    case "$(. /etc/os-release >/dev/null 2>&1; printf '%s' "${ID:-}")" in
      steamos|nixos|vanilla) return 0 ;;
    esac
  fi
  return 1
}

PKG_MGR=""
if command -v apt-get >/dev/null 2>&1; then
  PKG_MGR="apt"
elif command -v dnf >/dev/null 2>&1; then
  PKG_MGR="dnf"
elif command -v pacman >/dev/null 2>&1; then
  PKG_MGR="pacman"
fi

APT_UPDATED=0
install_packages() {
  case "$PKG_MGR" in
    apt)
      if [ "$APT_UPDATED" -eq 0 ]; then
        as_root apt-get update </dev/tty \
          || warn "O apt-get update falhou. Tentando instalar mesmo assim."
        APT_UPDATED=1
      fi
      as_root apt-get install -y "$@" </dev/tty
      ;;
    dnf)
      as_root dnf install -y "$@" </dev/tty
      ;;
    pacman)
      as_root pacman -S --needed --noconfirm "$@" </dev/tty
      ;;
    *)
      return 1
      ;;
  esac
}

install_command_text() {
  case "$PKG_MGR" in
    apt)    printf '%sapt-get install -y %s' "$SUDO_PREFIX" "$*" ;;
    dnf)    printf '%sdnf install -y %s' "$SUDO_PREFIX" "$*" ;;
    pacman) printf '%spacman -S --needed %s' "$SUDO_PREFIX" "$*" ;;
  esac
}

# Testa de verdade se o python3 consegue criar um ambiente virtual.
venv_works() {
  local tmp rc
  tmp="$(mktemp -d)"
  if python3 -m venv "$tmp/venv" >/dev/null 2>&1; then
    rc=0
  else
    rc=1
  fi
  rm -rf "$tmp"
  return "$rc"
}

# --- 1. Dependências do sistema ---------------------------------------------

NEED_GIT=0
NEED_PYTHON=0
NEED_VENV=0

if ! command -v git >/dev/null 2>&1; then
  NEED_GIT=1
fi
if ! command -v python3 >/dev/null 2>&1; then
  NEED_PYTHON=1
elif ! venv_works; then
  NEED_VENV=1
fi

if [ "$NEED_GIT" -eq 1 ] || [ "$NEED_PYTHON" -eq 1 ] || [ "$NEED_VENV" -eq 1 ]; then
  MISSING=()
  if [ "$NEED_GIT" -eq 1 ]; then MISSING+=("git"); fi
  if [ "$NEED_PYTHON" -eq 1 ]; then MISSING+=("python3"); fi
  if [ "$NEED_VENV" -eq 1 ]; then MISSING+=("suporte a ambientes virtuais do Python (venv)"); fi

  warn "Faltam dependências do sistema: $(IFS=,; printf '%s' "${MISSING[*]}" | sed 's/,/, /g')."

  if is_immutable_system; then
    die "Este parece ser um sistema imutável (a base é somente leitura), então não vou instalar pacotes por conta própria. Instale o que falta na imagem do sistema, com a ferramenta da sua distribuição (por exemplo, rpm-ostree), ou rode este instalador dentro de um contêiner (toolbox ou distrobox)."
  fi

  if [ -z "$PKG_MGR" ]; then
    die "Não reconheci o gerenciador de pacotes (suporto apt, dnf e pacman). Instale git, python3 (3.10 ou mais novo) e o suporte a venv, e rode o instalador de novo."
  fi

  PKGS=()
  case "$PKG_MGR" in
    apt)
      if [ "$NEED_GIT" -eq 1 ]; then PKGS+=("git"); fi
      if [ "$NEED_PYTHON" -eq 1 ]; then PKGS+=("python3" "python3-venv"); fi
      if [ "$NEED_VENV" -eq 1 ]; then PKGS+=("python3-venv"); fi
      ;;
    dnf)
      if [ "$NEED_GIT" -eq 1 ]; then PKGS+=("git"); fi
      if [ "$NEED_PYTHON" -eq 1 ]; then PKGS+=("python3" "python3-pip"); fi
      if [ "$NEED_VENV" -eq 1 ]; then PKGS+=("python3-pip"); fi
      ;;
    pacman)
      if [ "$NEED_GIT" -eq 1 ]; then PKGS+=("git"); fi
      if [ "$NEED_PYTHON" -eq 1 ]; then PKGS+=("python" "python-pip"); fi
      if [ "$NEED_VENV" -eq 1 ]; then PKGS+=("python-pip"); fi
      ;;
  esac

  INSTALL_CMD="$(install_command_text "${PKGS[@]}")"

  if ! can_root; then
    die "Preciso de permissão de administrador e o sudo não foi encontrado. Entre como root e rode: $(SUDO_PREFIX="" install_command_text "${PKGS[@]}")"
  fi

  if [ "$HAS_TTY" -ne 1 ]; then
    die "Não há terminal interativo para pedir confirmação. Instale manualmente com: $INSTALL_CMD"
  fi

  info "Comando que será executado:"
  info "    $INSTALL_CMD"
  if [ -n "$SUDO_PREFIX" ]; then
    QUESTION="Instalar agora com sudo? (ele pode pedir a sua senha)"
  else
    QUESTION="Instalar agora?"
  fi
  if ! ask_yn "$QUESTION" "S"; then
    die "Instalação cancelada. Instale manualmente com: $INSTALL_CMD"
  fi

  install_packages "${PKGS[@]}" \
    || die "Falha ao instalar os pacotes. Tente manualmente com: $INSTALL_CMD"
  ok "Pacotes instalados"

  command -v git >/dev/null 2>&1 \
    || die "O git continua não encontrado após a instalação."
  command -v python3 >/dev/null 2>&1 \
    || die "O python3 continua não encontrado após a instalação."
  venv_works \
    || die "O python3 ainda não consegue criar ambientes virtuais (venv)."
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "É preciso Python 3.10 ou mais novo (encontrado: $(python3 -V 2>&1))."

# --- 2. Baixar ou atualizar o programa --------------------------------------

if [ -d "$INSTALL_DIR/.git" ]; then
  info "Instalação encontrada em $INSTALL_DIR. Atualizando..."
  git -C "$INSTALL_DIR" pull --ff-only --quiet \
    || die "Não foi possível atualizar. Verifique se há alterações locais em $INSTALL_DIR."
elif [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
  die "A pasta $INSTALL_DIR já existe e não é uma instalação do gem-cli. Use outra com GEM_CLI_DIR=/caminho."
else
  info "Baixando o programa para $INSTALL_DIR..."
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --quiet "$REPO_URL" "$INSTALL_DIR" \
    || die "Não foi possível baixar o repositório."
fi

[ -f "$INSTALL_DIR/$SCRIPT_NAME" ] \
  || die "O arquivo $SCRIPT_NAME não foi encontrado em $INSTALL_DIR."
ok "Programa pronto em $INSTALL_DIR"

# --- 3. Ambiente virtual e dependências do Python ---------------------------

if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  info "Criando o ambiente virtual..."
  python3 -m venv "$INSTALL_DIR/.venv" \
    || die "Falha ao criar o ambiente virtual."
fi

info "Instalando as dependências (google-genai e rich)..."
"$INSTALL_DIR/.venv/bin/python" -m pip install --quiet --upgrade google-genai rich \
  || die "Falha ao instalar as dependências."
ok "Dependências instaladas"

# --- 4. Chave de API ---------------------------------------------------------

ENV_FILE="$CONFIG_DIR/env"
mkdir -p "$CONFIG_DIR/prompts"

API_KEY=""
if [ -f "$ENV_FILE" ]; then
  ok "A configuração já existe em $ENV_FILE (não foi alterada)."
elif [ "$HAS_TTY" -eq 1 ]; then
  printf 'Cole sua chave do Gemini (grátis em https://aistudio.google.com/) ou Enter para pular: ' >/dev/tty
  read -rs API_KEY </dev/tty || API_KEY=""
  printf '\n' >/dev/tty
  API_KEY="${API_KEY//[[:space:]]/}"
  if [ -n "$API_KEY" ]; then
    ( umask 077; printf 'GEMINI_API_KEY=%s\n' "$API_KEY" > "$ENV_FILE" )
    chmod 600 "$ENV_FILE"
    ok "Chave salva em $ENV_FILE"
  else
    warn "Nenhuma chave informada. Crie o arquivo depois:"
    info "    echo \"GEMINI_API_KEY=sua_chave\" > \"$ENV_FILE\" && chmod 600 \"$ENV_FILE\""
  fi
else
  warn "Sem terminal interativo, então a chave não foi pedida. Crie o arquivo depois:"
  info "    echo \"GEMINI_API_KEY=sua_chave\" > \"$ENV_FILE\" && chmod 600 \"$ENV_FILE\""
fi

# --- 5. Comando "gem-cli" em ~/.local/bin -----------------------------------

BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/gem-cli"
LAUNCHER_MARK="# gem-cli launcher"
LAUNCHER_READY=0

mkdir -p "$BIN_DIR"

# Nunca sobrescreve um arquivo que não seja o launcher do próprio gem-cli.
if { [ -e "$LAUNCHER" ] || [ -L "$LAUNCHER" ]; } && ! grep -qF "$LAUNCHER_MARK" "$LAUNCHER" 2>/dev/null; then
  warn "Já existe outro arquivo em $LAUNCHER que não foi criado pelo gem-cli. Não sobrescrevi."
  info "    Para usar o comando gem-cli, apague ou renomeie esse arquivo e rode o instalador de novo."
else
  {
    printf '#!/usr/bin/env bash\n%s\n' "$LAUNCHER_MARK"
    printf 'exec %q %q "$@"\n' "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/$SCRIPT_NAME"
  } > "$LAUNCHER"
  chmod +x "$LAUNCHER"
  ok "Comando criado em $LAUNCHER"
  LAUNCHER_READY=1
fi

IN_PATH=0
case ":$PATH:" in
  *":$BIN_DIR:"*) IN_PATH=1 ;;
esac

if [ "$LAUNCHER_READY" -eq 1 ] && [ "$IN_PATH" -eq 0 ]; then
  warn "A pasta $BIN_DIR não está no seu PATH, então o comando gem-cli ainda não é encontrado."
  case "${SHELL:-}" in
    */fish)
      info "    Rode uma vez: fish_add_path \"$BIN_DIR\""
      ;;
    */zsh)
      info "    Coloque esta linha no seu ~/.zshrc: export PATH=\"\$HOME/.local/bin:\$PATH\""
      ;;
    *)
      info "    Coloque esta linha no seu ~/.bashrc: export PATH=\"\$HOME/.local/bin:\$PATH\""
      ;;
  esac
  info "    Depois abra um novo terminal. Em muitas distribuições, sair e entrar de novo na sessão também resolve."
fi

# Atalho (alias) das versões antigas do instalador: não é mais necessário.
for RC_FILE in "$HOME/.bashrc" "$HOME/.zshrc"; do
  if [ -f "$RC_FILE" ] && grep -qE '^alias gemini=.*gem-cli' "$RC_FILE" 2>/dev/null; then
    warn "Encontrei o atalho antigo \"gemini\" em $RC_FILE. Ele não é mais necessário: pode apagar a linha alias gemini=... (e o comentário # gem-cli acima dela, se houver)."
  fi
done

# --- 6. Opcional: área de transferência (/copy e /code) ---------------------

if ! command -v wl-copy >/dev/null 2>&1 \
  && ! command -v xclip >/dev/null 2>&1 \
  && ! command -v xsel >/dev/null 2>&1 \
  && ! command -v pbcopy >/dev/null 2>&1; then
  if [ "$HAS_TTY" -eq 1 ] && [ -n "$PKG_MGR" ] && can_root && ! is_immutable_system; then
    if [ -n "${WAYLAND_DISPLAY:-}" ]; then
      CLIP_PKG="wl-clipboard"
    else
      CLIP_PKG="xclip"
    fi
    info "Opcional: para os comandos /copy e /code é preciso um programa de copiar texto."
    info "    $(install_command_text "$CLIP_PKG")"
    if ask_yn "Instalar $CLIP_PKG agora${SUDO_PREFIX:+ com sudo}?" "N"; then
      install_packages "$CLIP_PKG" \
        || warn "Não foi possível instalar o $CLIP_PKG. Os comandos /copy e /code não vão funcionar."
    fi
  else
    warn "Opcional: instale wl-copy (Wayland) ou xclip (X11) para usar os comandos /copy e /code."
  fi
fi

# --- Fim ---------------------------------------------------------------------

echo
ok "Instalação concluída!"
if [ "$LAUNCHER_READY" -eq 1 ] && [ "$IN_PATH" -eq 1 ]; then
  info "Digite: gem-cli"
elif [ "$LAUNCHER_READY" -eq 1 ]; then
  info "Depois de colocar $BIN_DIR no PATH, abra um novo terminal e digite: gem-cli"
  info "Enquanto isso, você já pode rodar: $LAUNCHER"
else
  info "Para abrir o programa agora, rode: $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/$SCRIPT_NAME"
fi
