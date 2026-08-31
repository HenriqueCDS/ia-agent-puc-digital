<#
.SYNOPSIS
  Abre o psql no banco do Supabase lendo SUPABASE_DB_URL do .env.

.EXAMPLE
  .\scripts\psql.ps1                                  # sessão interativa
  .\scripts\psql.ps1 -c "select count(*) from langchain_pg_embedding;"
  .\scripts\psql.ps1 -f scripts\alguma_query.sql

  Qualquer argumento é repassado direto ao psql.

.NOTES
  Usa o cliente psql de dentro do container `agente-ead-db` (não precisa de
  psql instalado no Windows). Se o container não estiver de pé, sobe com
  `docker compose up -d`.
#>

$ErrorActionPreference = "Stop"

$envPath = Join-Path $PSScriptRoot "..\.env"
if (-not (Test-Path $envPath)) { throw "Não achei $envPath" }

$line = Select-String -Path $envPath -Pattern '^\s*SUPABASE_DB_URL\s*=' | Select-Object -First 1
if (-not $line) { throw "SUPABASE_DB_URL não está no .env" }
$url = ($line.Line -replace '^\s*SUPABASE_DB_URL\s*=\s*', '').Trim().Trim('"')

$flags = if ($args) { @() } else { @("-it") }   # -it só na sessão interativa
& docker exec @flags agente-ead-db psql $url @args
