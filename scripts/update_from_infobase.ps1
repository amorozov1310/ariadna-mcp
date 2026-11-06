<#
.SYNOPSIS
    Выгрузить конфигурацию (и расширения) из информационной базы 1С в файлы и
    обновить индекс Ариадны. Для запуска по расписанию (Планировщик заданий).

.DESCRIPTION
    1. Выгрузка Конфигуратором в пакетном режиме: /DumpConfigToFiles. Первый
       раз — полная; дальше — с -update -force: платформа переписывает только
       изменённые файлы (по ConfigDumpInfo.xml в каталоге выгрузки), поэтому
       каталог выгрузки (-DumpDir) нужно сохранять между запусками.
    2. Передача в Ариадну, один из двух способов:
       - в папку (-TargetRoot): robocopy /MIR в папку источников проекта
         Ариадны (локальную или общую \\сервер\...) — копируются только
         изменённые файлы;
       - архивом по сети (по умолчанию): ZIP по HTTP.
       Макеты (Template.*) не передаются: Ариадне они не нужны, а это больше
       половины объёма выгрузки типовой.
    3. Ариадна переиндексирует источник и ждёт окончания:
       POST /api/projects/<проект>/sources/<источник>/refresh.

    Проект и его источники (основная конфигурация, расширения) заранее
    создаёт администратор Ариадны. Подробно — docs/update.md.

    Коды возврата: 0 — всё обновлено; 1 — ошибка выгрузки; 2 — ошибка
    передачи или индексации; 3 — ошибка параметров.

.EXAMPLE
    # Файловая база, обновление архивом по сети, токен в переменной ARIADNA_TOKEN
    .\update_from_infobase.ps1 -InfoBaseFile "D:\Bases\MyConf" -User "Робот" -DumpDir "D:\Dumps\myconf" `
        -AriadnaUrl "http://ariadna.lan:19878" -Project myconf

.EXAMPLE
    # Серверная база с расширениями, выгрузка в общую папку сервера Ариадны
    .\update_from_infobase.ps1 -InfoBaseServer "srv1c\myconf" -User "Робот" -DumpDir "D:\Dumps\myconf" `
        -Extensions "Расширение1","Расширение2" -TargetRoot "\\ariadna\data\projects\myconf\sources" `
        -AriadnaUrl "http://ariadna.lan:19878" -Project myconf
#>
[CmdletBinding()]
param(
    # Адрес Web UI Ариадны, например http://ariadna.lan:19878
    [Parameter(Mandatory = $true)][string]$AriadnaUrl,
    # Проект Ариадны (id)
    [Parameter(Mandatory = $true)][string]$Project,
    # Источник Ариадны для основной конфигурации
    [string]$Source = 'main',

    # База: файловая (каталог с 1Cv8.1CD) или серверная ("сервер\база")
    [string]$InfoBaseFile,
    [string]$InfoBaseServer,
    [string]$User,
    # Пароль; если не задан — из переменной окружения ONEC_PASSWORD
    [string]$Password = $env:ONEC_PASSWORD,
    # Многопоточная выгрузка на сервере 1С (только для серверной базы)
    [switch]$ServerDump,

    # Расширения: имена в конфигурации. Источник Ариадны по умолчанию — то же
    # имя; иначе соответствие в -ExtensionSources @{ "ИмяРасширения" = "id_источника" }
    [string[]]$Extensions = @(),
    [hashtable]$ExtensionSources = @{},

    # Каталог выгрузки — сохраняется между запусками (для выгрузки только изменений)
    [Parameter(Mandatory = $true)][string]$DumpDir,
    # Передача в папку: каталог sources проекта Ариадны (в нём <источник>\xml);
    # не задан — передача архивом по сети
    [string]$TargetRoot,

    # Путь к 1cv8.exe; по умолчанию — самая новая установленная платформа
    [string]$Platform,
    # Личный токен Ариадны (роль user достаточно); по умолчанию — ARIADNA_TOKEN
    [string]$Token = $env:ARIADNA_TOKEN,
    [int]$TimeoutMinutes = 120,
    # Не выгружать — взять то, что уже лежит в DumpDir (выгрузку сделали иначе)
    [switch]$SkipDump
)

$ErrorActionPreference = 'Stop'
$LogDir = Join-Path $DumpDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$RunLog = Join-Path $LogDir ("update_{0:yyyyMMdd_HHmmss}.log" -f (Get-Date))

function Write-Log([string]$Message) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $Message
    Write-Host $line
    Add-Content -Path $RunLog -Value $line -Encoding UTF8
}

function Fail([int]$Code, [string]$Message) {
    Write-Log "ОШИБКА: $Message"
    exit $Code
}

# ---------------------------------------------------------------- параметры
if (-not $SkipDump) {
    if ([bool]$InfoBaseFile -eq [bool]$InfoBaseServer) {
        Fail 3 'Укажите базу: -InfoBaseFile (файловая) или -InfoBaseServer (серверная), что-то одно'
    }
    if ($ServerDump -and -not $InfoBaseServer) { Fail 3 '-ServerDump — только для серверной базы' }
    if (-not $Platform) {
        $Platform = Get-ChildItem -Path "$env:ProgramFiles\1cv8\*\bin\1cv8.exe", "${env:ProgramFiles(x86)}\1cv8\*\bin\1cv8.exe" -ErrorAction SilentlyContinue |
            Sort-Object { [version]($_.Directory.Parent.Name -replace '[^\d\.]', '') } -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $Platform -or -not (Test-Path $Platform)) { Fail 3 'Не найден 1cv8.exe — укажите -Platform' }
}
if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { Fail 3 'Нужен curl.exe (есть в Windows 10 1803 и новее)' }
$AriadnaUrl = $AriadnaUrl.TrimEnd('/')

# Что выгружаем и куда: основная конфигурация и расширения — свой каталог и свой источник.
$Items = @([pscustomobject]@{ Name = ''; Dir = (Join-Path $DumpDir 'config'); Source = $Source })
foreach ($ext in $Extensions) {
    $sid = if ($ExtensionSources.ContainsKey($ext)) { $ExtensionSources[$ext] } else { $ext }
    $Items += [pscustomobject]@{ Name = $ext; Dir = (Join-Path $DumpDir "ext\$ext"); Source = $sid }
}

# ---------------------------------------------------------------- выгрузка
function Invoke-Dump($Item) {
    New-Item -ItemType Directory -Force -Path $Item.Dir | Out-Null
    $what = if ($Item.Name) { "расширение «$($Item.Name)»" } else { 'конфигурацию' }
    $out = Join-Path $LogDir 'designer.log'
    $result = Join-Path $LogDir 'designer_result.txt'
    Remove-Item $result -ErrorAction SilentlyContinue
    $argv = @('DESIGNER')
    if ($InfoBaseFile) { $argv += @('/F', "`"$InfoBaseFile`"") } else { $argv += @('/S', "`"$InfoBaseServer`"") }
    if ($User) { $argv += @('/N', "`"$User`"") }
    if ($Password) { $argv += @('/P', "`"$Password`"") }
    $argv += @('/DisableStartupDialogs', '/DisableStartupMessages',
               '/DumpConfigToFiles', "`"$($Item.Dir)`"")
    if ($Item.Name) { $argv += @('-Extension', "`"$($Item.Name)`"") }
    # -update требует ConfigDumpInfo.xml прошлой выгрузки: без него — полная.
    $incremental = Test-Path (Join-Path $Item.Dir 'ConfigDumpInfo.xml')
    if ($incremental) { $argv += @('-update', '-force') }
    if ($ServerDump) { $argv += '-Server' }
    $argv += @('/Out', "`"$out`"", '/DumpResult', "`"$result`"")
    Write-Log ("Выгрузка: {0} ({1})" -f $what, $(if ($incremental) { 'только изменения' } else { 'полная' }))
    $p = Start-Process -FilePath $Platform -ArgumentList $argv -Wait -PassThru -WindowStyle Hidden
    $code = if (Test-Path $result) { (Get-Content $result -Raw).Trim() } else { '' }
    if ($p.ExitCode -ne 0 -or $code -ne '0') {
        if (Test-Path $out) { Get-Content $out -Encoding UTF8 -Tail 30 | ForEach-Object { Write-Log "  1С: $_" } }
        Fail 1 ("Выгрузка не удалась: {0}, код {1}, результат «{2}». Журнал Конфигуратора: {3}" -f $what, $p.ExitCode, $code, $out)
    }
}

if (-not $SkipDump) {
    Write-Log "Платформа: $Platform"
    foreach ($item in $Items) { Invoke-Dump $item }
}

# ---------------------------------------------------------------- передача и индексация
function New-SlimZip([string]$Dir, [string]$ZipPath) {
    # ZIP без макетов (Template.*): .NET ZipArchive, архивы больше 2 ГБ тоже.
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    if (Test-Path $ZipPath) { Remove-Item $ZipPath }
    $root = (Resolve-Path $Dir).Path.TrimEnd('\') + '\'
    $zip = [System.IO.Compression.ZipFile]::Open($ZipPath, 'Create')
    try {
        Get-ChildItem -Path $Dir -Recurse -File | Where-Object { $_.Name -notlike 'Template.*' } | ForEach-Object {
            $rel = $_.FullName.Substring($root.Length).Replace('\', '/')
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $_.FullName, $rel, [System.IO.Compression.CompressionLevel]::Fastest) | Out-Null
        }
    } finally { $zip.Dispose() }
}

function Invoke-Refresh($Item, [string]$ZipPath) {
    $url = "$AriadnaUrl/api/projects/$([uri]::EscapeDataString($Project))/sources/$([uri]::EscapeDataString($Item.Source))/refresh"
    $resp = Join-Path $LogDir 'ariadna_response.json'
    $curl = @('-sS', '-o', $resp, '-w', '%{http_code}', '-X', 'POST',
              '-F', 'wait=1', '-F', "timeout=$($TimeoutMinutes * 60)",
              '--max-time', "$($TimeoutMinutes * 60 + 600)")
    if ($Token) { $curl += @('-H', "Authorization: Bearer $Token") }
    if ($ZipPath) { $curl += @('-F', "xml_archive=@$ZipPath;type=application/zip") }
    $status = & curl.exe @curl $url
    $body = if (Test-Path $resp) { Get-Content $resp -Raw -Encoding UTF8 } else { '' }
    if ($status -ne '200') { Fail 2 "Ариадна ответила $status на обновление источника «$($Item.Source)»: $body" }
    $json = $body | ConvertFrom-Json
    Write-Log ("Источник «{0}»: {1}, {2} с; модулей {3}, процедур {4}" -f $Item.Source, $json.status,
               $json.duration_sec, $json.index.total_modules, $json.index.total_procedures)
}

$n = 0
foreach ($item in $Items) {
    $n++
    if (-not (Test-Path (Join-Path $item.Dir 'Configuration.xml'))) {
        Fail 2 "В $($item.Dir) нет выгрузки (Configuration.xml)"
    }
    if ($TargetRoot) {
        $target = Join-Path $TargetRoot "$($item.Source)\xml"
        Write-Log "Копирование в $target"
        # /MIR — зеркало: удалённые в базе файлы удаляются и в Ариадне.
        & robocopy.exe $item.Dir $target /MIR /XF 'Template.*' /XD logs /R:2 /W:5 /NP /NFL /NDL /NJH /NJS | Out-Null
        if ($LASTEXITCODE -ge 8) { Fail 2 "robocopy завершился с кодом $LASTEXITCODE" }
        Invoke-Refresh $item ''
    } else {
        # Имя архива — латиницей: у расширений имена обычно русские, а curl.exe
        # в Windows PowerShell 5.1 получает аргументы в кодировке ANSI.
        $zip = Join-Path $DumpDir ("upload_{0}.zip" -f $n)
        Write-Log "Архив для передачи: $zip"
        New-SlimZip $item.Dir $zip
        Write-Log ("Передача {0:N0} МБ в Ариадну" -f ((Get-Item $zip).Length / 1MB))
        Invoke-Refresh $item $zip
        Remove-Item $zip -ErrorAction SilentlyContinue
    }
}
Write-Log 'Готово: индекс Ариадны обновлён'
exit 0
