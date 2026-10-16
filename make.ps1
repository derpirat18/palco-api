<#
.SYNOPSIS
  Equivalente del Makefile para Windows.

.EXAMPLE
  .\make.ps1 instalar
  .\make.ps1 arriba
  .\make.ps1 migrar
  .\make.ps1 correr
  .\make.ps1 test
  .\make.ps1 migracion -m "agrega tabla de cortesias"
#>
param(
  [Parameter(Position = 0)]
  [ValidateSet('ayuda','instalar','arriba','abajo','migrar','migracion','correr','barrido','bandeja','test','lint','fmt','usuario')]
  [string]$Tarea = 'ayuda',

  [Alias('m')]
  [string]$Mensaje,

  # Solo para la tarea 'usuario'. Todos con nombre: pasar argumentos
  # posicionales extra a este script los enreda con $Mensaje.
  [string]$Email,
  [string]$Nombre,
  [string]$Rol,
  [int]$Cliente = 0,

  # Solo para las tareas 'barrido' y 'bandeja': una vuelta y termina.
  [switch]$UnaVez
)

$ErrorActionPreference = 'Stop'
$raiz = $PSScriptRoot
$py   = Join-Path $raiz '.venv\Scripts\python.exe'

function Requiere-Venv {
  if (-not (Test-Path $py)) {
    Write-Host "No hay entorno virtual. Corre primero: .\make.ps1 instalar" -ForegroundColor Red
    exit 1
  }
}

function Puerto-Abierto([int]$puerto) {
  # TcpClient directo en lugar de Test-NetConnection: mismo resultado en
  # PowerShell 5.1 y 7, sin depender del modulo NetTCPIP.
  try {
    $cliente = New-Object System.Net.Sockets.TcpClient
    $intento = $cliente.BeginConnect('127.0.0.1', $puerto, $null, $null)
    $abierto = $intento.AsyncWaitHandle.WaitOne(1500)
    if ($abierto) { $cliente.EndConnect($intento) }
    $cliente.Close()
    return $abierto
  } catch {
    return $false
  }
}

function Requiere-Servicios {
  # Sin esto, alembic y pytest tardan medio minuto y escupen cien lineas
  # de traceback para decir que Postgres no esta prendido.
  $faltan = @()
  if (-not (Puerto-Abierto 5432)) { $faltan += 'Postgres (5432)' }
  if (-not (Puerto-Abierto 6379)) { $faltan += 'Redis (6379)' }
  if ($faltan.Count -gt 0) {
    Write-Host ""
    Write-Host ("No responde: " + ($faltan -join ', ')) -ForegroundColor Red
    Write-Host "Corre primero: .\make.ps1 arriba" -ForegroundColor Yellow
    Write-Host ""
    exit 1
  }
}

switch ($Tarea) {

  'ayuda' {
    Write-Host ""
    Write-Host "  instalar   " -NoNewline -ForegroundColor Cyan; Write-Host "crea el entorno virtual e instala dependencias"
    Write-Host "  arriba     " -NoNewline -ForegroundColor Cyan; Write-Host "levanta Postgres, Redis y el buzon de pruebas en Docker"
    Write-Host "  abajo      " -NoNewline -ForegroundColor Cyan; Write-Host "detiene los servicios"
    Write-Host "  migrar     " -NoNewline -ForegroundColor Cyan; Write-Host "aplica migraciones pendientes"
    Write-Host "  migracion  " -NoNewline -ForegroundColor Cyan; Write-Host "genera migracion:  .\make.ps1 migracion -m 'descripcion'"
    Write-Host "  correr     " -NoNewline -ForegroundColor Cyan; Write-Host "arranca la API en http://localhost:8000"
    Write-Host "  barrido    " -NoNewline -ForegroundColor Cyan; Write-Host "cierra apartados y ordenes vencidas, reintenta cobros (-UnaVez: una vuelta)"
    Write-Host "  bandeja    " -NoNewline -ForegroundColor Cyan; Write-Host "manda los correos con boletos; se ven en http://localhost:8025 (-UnaVez: una vuelta)"
    Write-Host "  test       " -NoNewline -ForegroundColor Cyan; Write-Host "corre las pruebas"
    Write-Host "  lint       " -NoNewline -ForegroundColor Cyan; Write-Host "revisa estilo"
    Write-Host "  fmt        " -NoNewline -ForegroundColor Cyan; Write-Host "formatea"
    Write-Host "  usuario    " -NoNewline -ForegroundColor Cyan; Write-Host "da de alta personal: .\make.ps1 usuario -Email a@b.mx -Nombre 'X' -Rol admin_psd"
    Write-Host ""
  }

  'instalar' {
    if (-not (Test-Path $py)) {
      Write-Host "Creando entorno virtual..." -ForegroundColor Cyan
      python -m venv (Join-Path $raiz '.venv')
    }
    # pip no puede actualizarse invocandose como pip.exe en Windows:
    # hay que llamarlo como modulo de Python.
    & $py -m pip install --upgrade pip --quiet
    Write-Host "Instalando dependencias..." -ForegroundColor Cyan
    & $py -m pip install -e ".[dev]" --quiet
    $env_file = Join-Path $raiz '.env'
    if (-not (Test-Path $env_file)) {
      Copy-Item (Join-Path $raiz '.env.example') $env_file
      Write-Host "Se creo .env desde .env.example. Revisa las llaves antes de seguir." -ForegroundColor Yellow
    }
    Write-Host "Listo." -ForegroundColor Green
  }

  'arriba' {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
      Write-Host ""
      Write-Host "Docker Desktop no esta corriendo." -ForegroundColor Red
      Write-Host "Abrelo y espera a que diga 'Engine running' en verde." -ForegroundColor Yellow
      Write-Host ""
      exit 1
    }
    docker compose up -d
    Write-Host "Esperando a Postgres..." -ForegroundColor Cyan

    # Con ErrorActionPreference='Stop', lo que un comando nativo escriba
    # en stderr se vuelve excepcion. pg_isready escribe en stderr cada vez
    # que Postgres todavia no esta listo, que es exactamente lo que este
    # ciclo esta esperando: sin esto, la primera espera aborta el script.
    $previo = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
      $intentos = 0
      while ($true) {
        docker compose exec -T db pg_isready -U palco 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { break }

        # Si el contenedor ya murio, esperar no lo va a revivir.
        $cid = docker compose ps -aq db 2>&1 | Select-Object -First 1
        if ($cid) {
          $estado = docker inspect -f '{{.State.Status}}' $cid 2>&1
          if ($estado -match 'exited|dead') {
            Write-Host ""
            Write-Host "El contenedor de Postgres se detuvo ($estado)." -ForegroundColor Red
            Write-Host "Ultimas lineas del log:" -ForegroundColor Yellow
            docker compose logs --tail 40 db
            exit 1
          }
        }

        $intentos++
        if ($intentos -gt 60) {
          Write-Host ""
          Write-Host "Postgres no respondio en 60 segundos." -ForegroundColor Red
          docker compose logs --tail 40 db
          exit 1
        }
        Start-Sleep -Seconds 1
      }
    } finally {
      $ErrorActionPreference = $previo
    }
    Write-Host "Servicios arriba." -ForegroundColor Green
  }

  'abajo'  { docker compose down }

  'migrar' {
    Requiere-Venv
    Requiere-Servicios
    & $py -m alembic upgrade head
  }

  'migracion' {
    Requiere-Venv
    Requiere-Servicios
    if (-not $Mensaje) {
      Write-Host "Falta la descripcion: .\make.ps1 migracion -m 'descripcion'" -ForegroundColor Red
      exit 1
    }
    & $py -m alembic revision --autogenerate -m $Mensaje
  }

  'correr' {
    Requiere-Venv
    Requiere-Servicios
    & $py -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
  }

  'barrido' {
    Requiere-Venv
    Requiere-Servicios
    if ($UnaVez) {
      & $py -m app.workers.barrido --una-vez
    } else {
      & $py -m app.workers.barrido
    }
  }

  'bandeja' {
    Requiere-Venv
    Requiere-Servicios
    if ($UnaVez) {
      & $py -m app.workers.bandeja --una-vez
    } else {
      & $py -m app.workers.bandeja
    }
  }

  'test' {
    Requiere-Venv
    Requiere-Servicios
    & $py -m pytest
  }

  'lint' {
    Requiere-Venv
    & $py -m ruff check app tests scripts
    & $py -m ruff format --check app tests scripts
  }

  'fmt' {
    Requiere-Venv
    & $py -m ruff format app tests scripts
    & $py -m ruff check --fix app tests scripts
  }

  'usuario' {
    Requiere-Venv
    Requiere-Servicios
    if (-not $Email -or -not $Nombre -or -not $Rol) {
      Write-Host ""
      Write-Host "Uso:" -ForegroundColor Yellow
      Write-Host "  .\make.ps1 usuario -Email admin@psdigital.mx -Nombre 'JuanMa' -Rol admin_psd"
      Write-Host "  .\make.ps1 usuario -Email org@circo.mx -Nombre 'Nayeli' -Rol organizador -Cliente 1"
      Write-Host ""
      Write-Host "Roles: admin_psd, organizador, acceso"
      Write-Host ""
      exit 1
    }
    $argumentos = @($Email, $Nombre, $Rol)
    if ($Cliente -gt 0) { $argumentos += @('--cliente', "$Cliente") }
    & $py scripts\crear_usuario.py @argumentos
  }
}
