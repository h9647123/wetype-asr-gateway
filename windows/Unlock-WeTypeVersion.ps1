param(
    [string]$InstallDirectory = 'C:\Program Files\Tencent\WeType',
    [string]$BackupDirectory = 'C:\WeTypeASR\version-lock-backup'
)
$ErrorActionPreference='Stop'
$acl=Get-Acl $InstallDirectory
$acl.SetSecurityDescriptorSddlForm([IO.File]::ReadAllText((Join-Path $BackupDirectory 'directory-sddl.txt')))
Set-Acl $InstallDirectory $acl
# Only restore this snapshot if no later AppLocker policy changes must be preserved.
Set-AppLockerPolicy -XmlPolicy (Join-Path $BackupDirectory 'applocker-before.xml')
$hosts=Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
if(Test-Path (Join-Path $BackupDirectory 'hosts-before.txt')) {Copy-Item (Join-Path $BackupDirectory 'hosts-before.txt') $hosts -Force;Clear-DnsClientCache}
Write-Output 'Original directory ACL and local AppLocker policy restored. Perform manual upgrades in a maintenance window.'
