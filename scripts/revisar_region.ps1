<#
.SYNOPSIS
  Revisa que una region de AWS tenga todo lo que usa Palco, antes de crear
  produccion ahi.

.DESCRIPTION
  Mexico (mx-central-1) es una region nueva: puede no tener un tamano de
  base o de Valkey que usamos. Esto lo pregunta a AWS en un minuto, sin
  crear nada. Si algo sale FALTA, produccion va en Ohio (us-east-2).

  Tambien revisa SES en Ohio, de donde sale el correo (SES no existe en
  Mexico): si ya salio del modo de pruebas.

.EXAMPLE
  aws login --profile palco
  .\scripts\revisar_region.ps1 -Region mx-central-1
#>
param(
  [string]$Region = 'mx-central-1',
  [string]$Perfil = 'palco',
  [string]$ClaseBase = 'db.t4g.small',
  [string]$ClaseValkey = 'cache.t4g.micro'
)
# Sin 'Stop' a proposito (PowerShell 5.1): cada paso revisa $LASTEXITCODE.
$faltan = 0

function Revisar([string]$Que, [scriptblock]$Pregunta, [scriptblock]$Bien) {
  $r = & $Pregunta 2>$null
  $ok = ($LASTEXITCODE -eq 0) -and (& $Bien ($r | Out-String).Trim())
  if ($ok) { Write-Host ("  OK     " + $Que) -ForegroundColor Green }
  else {
    Write-Host ("  FALTA  " + $Que + "  (respuesta: " + (($r | Out-String).Trim()) + ")") -ForegroundColor Red
    $script:faltan++
  }
}

Write-Host "Region $Region" -ForegroundColor Cyan
Revisar 'Region activada en la cuenta' {
  aws account get-region-opt-status --region-name $Region --profile $Perfil --query RegionOptStatus --output text
} { param($v) $v -in @('ENABLED', 'ENABLED_BY_DEFAULT') }
Revisar 'Al menos 2 zonas de disponibilidad' {
  aws ec2 describe-availability-zones --region $Region --profile $Perfil --query 'length(AvailabilityZones)' --output text
} { param($v) [int]$v -ge 2 }
Revisar "PostgreSQL 16 en $ClaseBase" {
  aws rds describe-orderable-db-instance-options --engine postgres --db-instance-class $ClaseBase --region $Region --profile $Perfil --query "length(OrderableDBInstanceOptions[?starts_with(EngineVersion, '16')])" --output text
} { param($v) [int]$v -gt 0 }
Revisar 'Valkey 7.2 (ElastiCache)' {
  aws elasticache describe-cache-engine-versions --engine valkey --region $Region --profile $Perfil --query "length(CacheEngineVersions[?starts_with(EngineVersion, '7.2')])" --output text
} { param($v) [int]$v -gt 0 }
Revisar "Tamano de Valkey $ClaseValkey" {
  aws elasticache describe-reserved-cache-nodes-offerings --cache-node-type $ClaseValkey --region $Region --profile $Perfil --query 'length(ReservedCacheNodesOfferings)' --output text
} { param($v) [int]$v -gt 0 }
Revisar 'ECS (Fargate)' {
  aws ecs list-clusters --region $Region --profile $Perfil --query 'length(clusterArns)' --output text
} { param($v) $v -match '^\d+$' }
Revisar 'ECR (imagenes)' {
  aws ecr describe-repositories --region $Region --profile $Perfil --query 'length(repositories)' --output text
} { param($v) $v -match '^\d+$' }
Revisar 'Lista de IPs de CloudFront (para cerrar el balanceador)' {
  aws ec2 describe-managed-prefix-lists --region $Region --profile $Perfil --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing --query 'length(PrefixLists)' --output text
} { param($v) [int]$v -gt 0 }

Write-Host "Correo (SES en us-east-2)" -ForegroundColor Cyan
Revisar 'SES fuera del modo de pruebas' {
  aws sesv2 get-account --region us-east-2 --profile $Perfil --query ProductionAccessEnabled --output text
} { param($v) $v -eq 'True' }

Write-Host ''
if ($faltan -eq 0) { Write-Host "Todo listo para produccion en $Region." -ForegroundColor Green }
else { Write-Host "$faltan punto(s) pendientes. Si falta algo de la region, produccion va en us-east-2." -ForegroundColor Yellow }
