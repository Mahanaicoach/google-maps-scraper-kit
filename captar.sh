#!/usr/bin/env bash
# Captação de lojas no Mac/Linux: rode  ./captar.sh  no Terminal, dentro da pasta do kit.
# Precisa do Docker Desktop aberto.
set -e
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "[ERRO] O Docker Desktop não está aberto. Abra, espere 'Engine running' e tente de novo."
  exit 1
fi
[ -f .env ] || cp .env.example .env

echo "Iniciando o engine..."
docker compose up -d

read -rp "Cidade e UF (ex: Uberlandia, MG): " CIDADE
read -rp "Nichos separados por vírgula (Enter = todos os 9): " NICHOS
read -rp "Profundidade 1 a 10 (Enter = 3): " DEPTH
DEPTH=${DEPTH:-3}

ARGS=(--cidade "$CIDADE" --depth "$DEPTH")
[ -n "$NICHOS" ] && ARGS+=(--nichos "$NICHOS")
docker compose run --rm captador "${ARGS[@]}"

PASTA=$(ls -dt captacao-* 2>/dev/null | head -1)
[ -n "$PASTA" ] && { echo "Relatório em: $PASTA"; command -v open >/dev/null && open "$PASTA"; }
