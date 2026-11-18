param(
    [Parameter(Mandatory=$true)][string]$Root,
    [ValidateRange(0,2147483647)][int]$ExpectedCounter = 101
)
$ErrorActionPreference = 'Stop'
# Native readback only, in a private lab made by bridge.jet_lab. Never opens SGA.
if ([Environment]::Is64BitProcess) {
    throw 'Use Windows SysWOW64 PowerShell with the installed 32-bit DAO engine.'
}
$lab = (Resolve-Path -LiteralPath $Root).Path
$manifest = Get-Content -Raw -LiteralPath (Join-Path $lab 'manifest.json') | ConvertFrom-Json
if ($manifest.marker -ne 'servirepuestos-jet4-lab-v1') { throw 'Lab marker required' }
if (Test-Path -LiteralPath (Join-Path $lab 'transaction.json')) { throw 'Recover the lab in Python first' }
$databasePath = Join-Path $lab 'data.mdb'
$before = (Get-FileHash -Algorithm SHA256 -LiteralPath $databasePath).Hash
$engine = $null
$database = $null
$rows = $null
try {
    $engine = New-Object -ComObject DAO.DBEngine.36
    # Options=False is shared access; ReadOnly=True prevents native mutations.
    $database = $engine.OpenDatabase($databasePath, $false, $true)
    $rows = $database.OpenRecordset('PGPropiedades', 1, 4)
    $rows.Index = 'PrimaryKey'
    $rows.Seek('=', 'CounterFixture')
    if ($rows.NoMatch) { throw 'Counter fixture not found by its native primary key' }
    if ([string]$rows.Fields.Item('Valor').Value -ne [string]$ExpectedCounter) { throw 'Counter differs' }
    if ([int]$rows.Fields.Item('Guard').Value -ne 777) { throw 'Unrelated field differs' }
    $rows.Seek('=', 'OtherFixture')
    if ($rows.NoMatch -or [string]$rows.Fields.Item('Valor').Value -ne '666' -or
        [int]$rows.Fields.Item('Guard').Value -ne 888) { throw 'Unrelated row differs' }
    $version = [string]$engine.Version
} finally {
    if ($null -ne $rows) { $rows.Close(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($rows) }
    if ($null -ne $database) { $database.Close(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($database) }
    if ($null -ne $engine) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($engine) }
}
$after = (Get-FileHash -Algorithm SHA256 -LiteralPath $databasePath).Hash
if ($before -ne $after) { throw 'Native readback changed the file' }
[ordered]@{
    status = 'native_readback_passed'
    dao_version = $version
    database_sha256 = $after
    expected_counter = $ExpectedCounter
    primary_key_seek_verified = $true
    unrelated_values_verified = $true
    native_shared_writer_certified = $false
} | ConvertTo-Json
