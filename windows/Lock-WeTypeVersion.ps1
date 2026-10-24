param(
    [string]$InstallDirectory = 'C:\Program Files\Tencent\WeType',
    [string]$BackupDirectory = 'C:\WeTypeASR\version-lock-backup',
    [string]$InstallerPath
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (!(Test-Path $InstallDirectory -PathType Container)) { throw 'WeType directory not found.' }
if (!(Get-Command Get-AppLockerPolicy -ErrorAction SilentlyContinue)) { throw 'AppLocker support is required; this script targets Windows Enterprise LTSC.' }
if (Test-Path (Join-Path $BackupDirectory 'directory-sddl.txt')) { throw 'A backup already exists. Inspect current protection before applying again.' }
New-Item $BackupDirectory -ItemType Directory -Force | Out-Null
& icacls.exe $BackupDirectory /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to protect backup directory.' }
$oldAcl = Get-Acl $InstallDirectory
[IO.File]::WriteAllText((Join-Path $BackupDirectory 'directory-sddl.txt'),$oldAcl.Sddl)
$oldPolicy = Get-AppLockerPolicy -Local -Xml
[IO.File]::WriteAllText((Join-Path $BackupDirectory 'applocker-before.xml'),$oldPolicy)
[xml]$policy = $oldPolicy
$collection = $policy.AppLockerPolicy.SelectSingleNode('RuleCollection[@Type="Exe"]')
if (!$collection) {
    $collection=$policy.CreateElement('RuleCollection')
    $collection.SetAttribute('Type','Exe'); $collection.SetAttribute('EnforcementMode','Enabled')
    [void]$policy.AppLockerPolicy.AppendChild($collection)
    $allow=$policy.CreateElement('FilePathRule')
    $allow.SetAttribute('Id',[guid]::NewGuid().ToString());$allow.SetAttribute('Name','Keep existing executable behavior')
    $allow.SetAttribute('Description','Only targeted WeType installer denies are introduced')
    $allow.SetAttribute('UserOrGroupSid','S-1-1-0');$allow.SetAttribute('Action','Allow')
    $conditions=$policy.CreateElement('Conditions');$path=$policy.CreateElement('FilePathCondition');$path.SetAttribute('Path','*')
    [void]$conditions.AppendChild($path);[void]$allow.AppendChild($conditions);[void]$collection.AppendChild($allow)
} elseif ($collection.GetAttribute('EnforcementMode') -ne 'Enabled') {
    throw 'Existing EXE AppLocker policy is not enforced; refusing to change unrelated policy behavior.'
}
foreach($pattern in @('*\WeTypeSetup*.exe','*\WeTypeInstaller*.exe')) {
    $rule=$policy.CreateElement('FilePathRule')
    $rule.SetAttribute('Id',[guid]::NewGuid().ToString());$rule.SetAttribute('Name','Block WeType upgrade installer '+$pattern)
    $rule.SetAttribute('Description','Version pinning; does not block wetype_update.exe required for ASR')
    $rule.SetAttribute('UserOrGroupSid','S-1-1-0');$rule.SetAttribute('Action','Deny')
    $conditions=$policy.CreateElement('Conditions');$path=$policy.CreateElement('FilePathCondition');$path.SetAttribute('Path',$pattern)
    [void]$conditions.AppendChild($path);[void]$rule.AppendChild($conditions);[void]$collection.AppendChild($rule)
}
# By default AppLocker ignores SYSTEM/service launches, which is how ASR runs.
# Explicitly include service processes while retaining the existing executable rules.
$extensions=$collection.SelectSingleNode('RuleCollectionExtensions')
if (!$extensions) {$extensions=$policy.CreateElement('RuleCollectionExtensions');[void]$collection.AppendChild($extensions)}
$threshold=$extensions.SelectSingleNode('ThresholdExtensions')
if (!$threshold) {$threshold=$policy.CreateElement('ThresholdExtensions');[void]$extensions.AppendChild($threshold)}
$services=$threshold.SelectSingleNode('Services')
if (!$services) {$services=$policy.CreateElement('Services');[void]$threshold.AppendChild($services)}
$services.SetAttribute('EnforcementMode','Enabled')
$redstone=$extensions.SelectSingleNode('RedstoneExtensions')
if (!$redstone) {$redstone=$policy.CreateElement('RedstoneExtensions');[void]$extensions.AppendChild($redstone)}
$systemApps=$redstone.SelectSingleNode('SystemApps')
if (!$systemApps) {$systemApps=$policy.CreateElement('SystemApps');[void]$redstone.AppendChild($systemApps)}
$systemApps.SetAttribute('Allow','Enabled')
$policyPath=Join-Path $BackupDirectory 'applocker-version-lock.xml'
$policy.Save($policyPath)
Set-AppLockerPolicy -XmlPolicy $policyPath
& sc.exe config appidsvc start= auto | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to configure Application Identity autostart.' }
Start-Service AppIDSvc
$rights=[Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
$everyone=New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
$deny=New-Object Security.AccessControl.FileSystemAccessRule($everyone,$rights,'ContainerInherit,ObjectInherit','None','Deny')
$oldAcl.AddAccessRule($deny)
Set-Acl $InstallDirectory $oldAcl
$hosts=Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
Copy-Item $hosts (Join-Path $BackupDirectory 'hosts-before.txt')
if (![regex]::IsMatch([IO.File]::ReadAllText($hosts),'(?im)^\s*(?:0\.0\.0\.0|127\.0\.0\.1)\s+download\.z\.weixin\.qq\.com(?:\s|$)')) {
    [IO.File]::AppendAllText($hosts,"`r`n# WeType ASR pinned-version installer download block`r`n0.0.0.0 download.z.weixin.qq.com`r`n")
}
Clear-DnsClientCache
Write-Output 'WeType installer execution blocked and program directory write/delete denied.'
Write-Output 'wetype_update.exe remains runnable for ASR; no application executable was renamed or patched.'
Write-Output 'Known official installer download host is blocked; other Tencent/ASR hosts are unchanged.'
Write-Output ('Backup: '+$BackupDirectory)
if ($InstallerPath) { Test-AppLockerPolicy -XmlPolicy $policyPath -Path $InstallerPath -User 'S-1-5-18' | Select-Object FilePath,PolicyDecision | Format-Table }
