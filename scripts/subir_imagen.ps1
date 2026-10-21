<#
.SYNOPSIS
  Construye la imagen de Palco con el commit actual y la sube a ECR.

.DESCRIPTION
  La etiqueta es el commit (git rev-parse --short HEAD): asi se sabe
  exactamente que codigo corre en AWS. ECR no deja reemplazar una etiqueta
  (IMMUTABLE), por eso exige que no haya cambios sin commit.

  El login a ECR usa una configuracion de Docker temporal: el almacen de
  credenciales de Windows (wincred) no guarda el token de ECR, que es muy
  largo, y falla con "The stub received bad data". Al terminar se borra.

.EXAMPLE
  aws login --profile palco
  .\scripts\subir_imagen.ps1
#>
param(
  [string]$Perfil = 'palco',
  [string]$Region = 'us-east-2',
  [string]$Repositorio = 'palco-sandbox'
)
# Sin 'Stop' a proposito: en PowerShell 5.1 cualquier aviso que un programa
# (aws, docker) escriba en su salida de errores se volveria excepcion aunque
# el programa termine bien. Cada paso revisa $LASTEXITCODE.
Set-Location (Split-Path -Parent $PSScriptRoot)

if (git status --porcelain) {
  throw 'Hay cambios sin commit. La imagen lleva el nombre del commit: primero haz commit.'
}
$tag = (git rev-parse --short=7 HEAD).Trim()

$cuenta = (aws sts get-caller-identity --profile $Perfil --query Account --output text)
if ($LASTEXITCODE -ne 0) { throw "Sin permiso de AWS. Corre primero: aws login --profile $Perfil" }
$registro = "$($cuenta.Trim()).dkr.ecr.$Region.amazonaws.com"
$imagen = "$registro/${Repositorio}:$tag"

cmd /c "aws ecr describe-images --profile $Perfil --region $Region --repository-name $Repositorio --image-ids imageTag=$tag >nul 2>&1"
if ($LASTEXITCODE -eq 0) {
  Write-Host "La imagen $tag ya esta en ECR. No hay nada que subir." -ForegroundColor Yellow
  Write-Host "imagen_tag=$tag"
  return
}

$docker = (Get-Command docker).Source
Write-Host "Construyendo $imagen ..." -ForegroundColor Cyan
& $docker build --platform linux/amd64 -t $imagen .
if ($LASTEXITCODE -ne 0) { throw 'Fallo la construccion de la imagen.' }

$config = Join-Path $env:TEMP 'docker-ecr'
New-Item -ItemType Directory -Force $config | Out-Null
'{"auths":{}}' | Set-Content -Encoding ascii (Join-Path $config 'config.json')
$antes = @{ DOCKER_CONFIG = $env:DOCKER_CONFIG; DOCKER_HOST = $env:DOCKER_HOST; Path = $env:Path }
try {
  $env:DOCKER_CONFIG = $config
  $env:DOCKER_HOST = 'npipe:////./pipe/dockerDesktopLinuxEngine'
  # Sin la carpeta de Docker en el PATH no se usa docker-credential-wincred.
  $env:Path = (($env:Path -split ';') | Where-Object { $_ -notmatch 'Docker' }) -join ';'

  # La tuberia de PowerShell 5.1 altera el token; la de cmd no.
  cmd /c "aws ecr get-login-password --profile $Perfil --region $Region | `"$docker`" login --username AWS --password-stdin $registro"
  if ($LASTEXITCODE -ne 0) { throw 'Fallo el login a ECR.' }

  Write-Host "Subiendo $imagen ..." -ForegroundColor Cyan
  & $docker push $imagen
  if ($LASTEXITCODE -ne 0) { throw 'Fallo la subida a ECR.' }
}
finally {
  $env:DOCKER_CONFIG = $antes.DOCKER_CONFIG
  $env:DOCKER_HOST = $antes.DOCKER_HOST
  $env:Path = $antes.Path
  Remove-Item -Recurse -Force $config -ErrorAction SilentlyContinue
}

Write-Host "Listo. Imagen en ECR con la etiqueta $tag." -ForegroundColor Green
Write-Host "imagen_tag=$tag"
