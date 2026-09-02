<#
.SYNOPSIS
  Abre o psql no banco do Supabase lendo SUPABASE_DB_URL do .env.

.EXAMPLE
  .\scripts\psql.ps1                                  # sessão interativa
  .\scripts\psql.ps1 -c "select count(*) from langchain_pg_embedding;"
  .\scripts\psql.ps1 -f scripts\alguma_query.sql

  Qualquer argumento é repassado direto ao psql.

.NOTES
  Usa o psql de dentro do container `agente-ead-db` — nada precisa ser
  instalado no Windows. Por isso SUPABASE_DB_URL tem que ser a Session pooler
  (aws-0-*.pooler.supabase.com): a conexão direta db.<ref>.supabase.co é
  IPv6-only e não roteia de dentro de um container.
  Container fora do ar: `docker compose up -d`.
#>

$ErrorActionPreference = "Stop"

$envPath = Join-Path $PSScriptRoot "..\.env"
if (-not (Test-Path $envPath)) { throw "Não achei $envPath" }

$line = Select-String -Path $envPath -Pattern '^\s*SUPABASE_DB_URL\s*=' | Select-Object -First 1
if (-not $line) { throw "SUPABASE_DB_URL não está no .env" }
$url = ($line.Line -replace '^\s*SUPABASE_DB_URL\s*=\s*', '').Trim().Trim('"')

$flags = if ($args) { @() } else { @("-it") }   # -it só na sessão interativa
& docker exec @flags agente-ead-db psql @args $url
