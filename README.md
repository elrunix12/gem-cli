# Chatbot no Terminal

Use o chatbot Gemini (a IA do Google) direto no terminal. Você digita a pergunta e a resposta aparece na hora, já bem formatada.

> **Aviso:** este é um projeto não oficial e não tem nenhuma relação com o Google. "Gemini" é uma marca do Google, usada aqui apenas para indicar com qual serviço o programa conversa.

## O que você precisa

- Python instalado no computador (versão 3.10 ou mais nova)
- Git instalado
- Uma chave do Gemini, que é grátis. Pegue em https://aistudio.google.com/
- Duas bibliotecas de Python, que o passo 2 da instalação já instala para você:
  - `google-genai`: faz a conversa com o Gemini
  - `rich`: deixa a resposta bonita e formatada no terminal
- Opcional, só para os comandos `/copy` e `/code` funcionarem: um programa de copiar texto.
  - Linux com Wayland: `wl-copy`
  - Linux com X11: `xclip` ou `xsel`
  - Mac (não testado): o `pbcopy` já vem instalado

## Sistemas testados

O programa foi testado exclusivamente no Debian. Espera-se que funcione em outras distribuições Linux, mas não há garantia. O funcionamento no macOS e no Windows não foi verificado.

## Como instalar

**1. Baixe o programa** para a pasta `~/.local/share/gem-cli`:

```bash
git clone https://github.com/elrunix12/gem-cli.git ~/.local/share/gem-cli
```

Se preferir, você pode instalar em outra pasta. É só trocar `~/.local/share/gem-cli` pelo caminho que quiser, e usar o mesmo caminho nos passos 2 e 4 e na atualização.

**2. Instale o que o programa usa.** O ambiente (`.venv`) fica dentro da mesma pasta:

```bash
python3 -m venv ~/.local/share/gem-cli/.venv
~/.local/share/gem-cli/.venv/bin/pip install google-genai rich
```

**3. Guarde a sua chave** num arquivo de configuração:

```bash
mkdir -p ~/.config/python/gem-cli/prompts
echo "GEMINI_API_KEY=cole_sua_chave_aqui" > ~/.config/python/gem-cli/env
chmod 600 ~/.config/python/gem-cli/env
```

Troque `cole_sua_chave_aqui` pela chave que você pegou no site. O último comando deixa o arquivo visível só para você.

**4. Crie um atalho** para abrir o programa só digitando `gemini`. Coloque esta linha no arquivo `~/.bashrc` (ou `~/.zshrc`, se você usa zsh):

```bash
alias gemini='~/.local/share/gem-cli/.venv/bin/python ~/.local/share/gem-cli/gem-cli.py'
```

Depois feche e abra o terminal de novo.

## Como atualizar

```bash
cd ~/.local/share/gem-cli && git pull
```

## Como desinstalar

Apague a pasta do programa e a pasta de configuração:

```bash
rm -rf ~/.local/share/gem-cli
rm -rf ~/.config/python/gem-cli
```

A segunda linha apaga também a sua chave e os seus arquivos de "personalidade". Depois, remova a linha do `alias gemini=...` do arquivo `~/.bashrc` (ou `~/.zshrc`). As conversas salvas com `/save` ficam em `~/Documentos/Gemini`; apague essa pasta também se quiser.

## Como usar

Abra o programa e escreva sua pergunta:

```bash
gemini
```

Ao abrir, ele faz duas perguntas rápidas:

1. **Arquivo de prompt:** pode apertar Enter para pular.
2. **Respostas curtas?** Digite `s` (ou Enter) para respostas diretas, ou `n` para respostas mais completas.

Depois é só conversar. Ele lembra do que vocês já falaram.

Se quiser pular essas perguntas:

```bash
gemini --curto     # respostas curtas
gemini --longo     # respostas detalhadas
```

## Comandos úteis

Digite estes comandos no lugar de uma pergunta:

| Comando | O que faz |
|---|---|
| `/copy` | Copia a última resposta |
| `/code` | Copia só os códigos da última resposta |
| `/save` | Salva a conversa num arquivo |
| `/curto` | Liga ou desliga as respostas curtas |
| `/reset` | Apaga a conversa e começa do zero |
| `!comando` | Roda um comando do terminal (ex.: `!ls`) |
| `sair` | Fecha o programa |

Para parar uma resposta no meio, aperte `Ctrl+C`.

## Trocar o modelo

O modelo é o "cérebro" do Gemini que responde você. Para usar outro, adicione uma linha no arquivo `~/.config/python/gem-cli/env`:

```
GEMINI_MODEL=nome-do-modelo
```

Ou use só uma vez:

```bash
gemini --model nome-do-modelo
```

## Dar uma "personalidade" ao Gemini (opcional)

Crie um arquivo de texto na pasta `~/.config/python/gem-cli/prompts` explicando como ele deve se comportar. Por exemplo, um arquivo chamado `revisor.md` com o texto:

```
Você é um revisor de código. Responda sempre em português.
```

Depois abra o programa com:

```bash
gemini --prompt revisor.md
```

## Onde ficam as coisas

| O quê | Onde |
|---|---|
| O programa e o `.venv` | `~/.local/share/gem-cli` |
| Sua chave e configurações | `~/.config/python/gem-cli/env` |
| Seus arquivos de "personalidade" | `~/.config/python/gem-cli/prompts` |
| Conversas salvas com `/save` | `~/Documentos/Gemini` |

## Se algo der errado

| O que aconteceu | O que fazer |
|---|---|
| "Servidor ocupado" ou erro 503 | O Google está sobrecarregado. O programa tenta de novo sozinho. Se continuar, espere um pouco ou troque de modelo. |
| Erro 404 | O nome do modelo está errado. Confira o arquivo `env`. |
| Erro 429 | Você passou do limite gratuito. Espere um tempo. |
| Erro no meio da conversa | Digite `/reset` para começar uma conversa nova. |
| "Dependência ausente" | Refaça o passo 2 da instalação e confira se o atalho usa o Python do `.venv`. |
| `/copy` não funciona | Instale `wl-copy` (Wayland) ou `xclip` (X11). |

## Privacidade

Tudo o que você digita é enviado ao Google para gerar a resposta. No plano gratuito do Google AI Studio, o Google pode usar essas conversas para treinar os modelos dele. Por isso, não envie senhas, dados pessoais ou informações confidenciais. Para saber os detalhes, consulte a política de privacidade e os termos de uso da API do Gemini no site do Google.

O comando `/save` grava a conversa em texto puro no seu computador, sem nenhuma proteção.

## Cuidado com a sua chave

A chave é como uma senha. Não compartilhe o arquivo `env` com ninguém e não envie para a internet. Se ela vazar, gere uma nova no site do Google e apague a antiga.

## Licença

Este projeto usa a licença MIT. Você pode usar, copiar e modificar à vontade. Veja o arquivo `LICENSE` para os detalhes.