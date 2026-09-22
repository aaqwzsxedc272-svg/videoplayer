

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
# Straight to the console, never through the pipeline: a function whose
# diagnostics use Write-Output has them captured by whoever assigns its
# return value, which is exactly how the await failures went missing.
function Say($m) { [Console]::Out.WriteLine('#diag ' + $m) }
function Emit($m) { [Console]::Out.WriteLine($m) }

# Some WinRT enumeration paths behave differently off an MTA thread, and
# that failure looks exactly like a bad argument. Say which one we are on.
try { Say ('apartment=' + [System.Threading.Thread]::CurrentThread.GetApartmentState()) } catch {}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    Say 'winrt=loaded'
} catch { Say ('winrt=failed:' + $_.Exception.Message) }

$asTask = $null
try {
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
} catch { Say ('astask=failed:' + $_.Exception.Message) }
if ($asTask) { Say 'astask=ok' } else { Say 'astask=missing' }

function Await($op, $type) {
    if (-not $asTask) { return $null }
    try {
        # MethodInfo.Invoke sees the PSObject wrapper, not the object inside
        # it, so a plain method call would work here and this one does not.
        $opBase = $op
        try { $opBase = $op.PsObject.BaseObject } catch {}
        $t = $asTask.MakeGenericMethod($type).Invoke($null, @($opBase))
        # Windows PowerShell runs this probe on STA. Blocking that STA with
        # Task.Wait prevents the WinRT completion from being delivered, so
        # wait from a pool thread instead and only join that waiter here.
        $box = @{ result = $null; error = $null; done = $false }
        $waiter = [System.Threading.Tasks.Task]::Run([Action]{
            try { $box.result = $t.GetAwaiter().GetResult() }
            catch { $box.error = $_.Exception; $box.done = $true; return }
            $box.done = $true
        })
        try { $waiter.Wait(10000) | Out-Null } catch {
            $e = $_.Exception
            try { if ($e.InnerException) { $e = $e.InnerException } } catch {}
            Say ('await=failed:' + $e.Message)
            return $null
        }
        if ($box.error) {
            $e = $box.error
            try { if ($e.InnerException) { $e = $e.InnerException } } catch {}
            Say ('await=failed:' + $e.Message); return $null
        }
        if (-not $box.done) { Say 'await=timeout'; return $null }
        return $box.result
    } catch { Say ('await=failed:' + $_.Exception.Message); return $null }
}

# Both projections, registered before either type is used: PowerShell 5.1
# only resolves WinRT bracket syntax once the type has been named in
# assembly-qualified form.
$pnType = $null
$diType = $null
try {
    $null = [Windows.Devices.Enumeration.Pnp.PnpObject, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
    Say 'projection=registered'
    $pnType = [Windows.Devices.Enumeration.Pnp.PnpObject]
    Say 'pnp=loaded'
} catch { Say ('pnp=failed:' + $_.Exception.Message) }
try {
    $null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
    $diType = [Windows.Devices.Enumeration.DeviceInformation]
    Say 'di=loaded'
} catch { Say ('di=failed:' + $_.Exception.Message) }

# The Pnp namespace is documented as superseded by Windows.Devices.
# Enumeration, so both are tried rather than betting on one again.
$diKind = $null
$pnKind = $null
try {
    $diKind = [Windows.Devices.Enumeration.DeviceInformationKind]
    $names = @([Enum]::GetNames($diKind))
    Say ('enumnames=' + ($names -join ','))
} catch { Say ('enumnames=failed:' + $_.Exception.Message) }
try { $pnKind = [Windows.Devices.Enumeration.Pnp.PnpObjectType] } catch {}

function MakeProps([string[]]$wanted) {
    # The projected WinRT binder wants String[], not a PSObject wrapping a
    # generic List[string]. Build the exact array type before FindAllAsync.
    $vals = @($wanted | Where-Object { $_ })
    $out = New-Object 'string[]' $vals.Count
    for ($i = 0; $i -lt $vals.Count; $i++) { $out[$i] = [string]$vals[$i] }
    return ,$out
}

# Reflection dispatch, arguments unwrapped. The overload binder cannot see
# the projected signatures, so the method is chosen by parameter count.
function CallFindAll($type, $argValues) {
    if (-not $type) { return $null }
    $mi = $null
    try {
        $mi = $type.GetMethods() | Where-Object { $_.Name -eq 'FindAllAsync' -and $_.GetParameters().Count -eq $argValues.Count } | Select-Object -First 1
    } catch {}
    if (-not $mi) { return $null }
    # Built by index, never with +=: appending a List[string] to a
    # PowerShell array enumerates it, so the property list arrived as one
    # String per property and the binder was handed a String where it
    # wanted IEnumerable`1[System.String].
    $raw = New-Object 'object[]' $argValues.Count
    for ($i = 0; $i -lt $argValues.Count; $i++) {
        $b = $argValues[$i]
        try { $b = $argValues[$i].PsObject.BaseObject } catch {}
        $raw[$i] = $b
    }
    return $mi.Invoke($null, $raw)
}

$BAT = '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2'

function TryCombo($label, $api, $kv, [string[]]$propNames) {
    $p = MakeProps $propNames
    # Try the projected static method first. MethodInfo.Invoke returns a
    # bare __ComObject on Windows PowerShell, which cannot be handed to
    # AsTask as the projected IAsyncOperation even when the invocation itself
    # succeeded. The direct call preserves the projection.
    $direct = $null
    try {
        # Do not use @('', $p, $kv): PowerShell flattens the String[] and
        # shifts the projected overload arguments. Build an object[] by
        # index, just as CallFindAll does.
        $directArgs = New-Object 'object[]' 3
        if ($api -eq 'di') {
            $directArgs[0] = ''
            $directArgs[1] = $p
            $directArgs[2] = $kv
            $direct = $diType::FindAllAsync($directArgs[0], $directArgs[1], $directArgs[2])
        } else {
            $directArgs[0] = $kv
            $directArgs[1] = $p
            $directArgs[2] = ''
            $direct = $pnType::FindAllAsync($directArgs[0], $directArgs[1], $directArgs[2])
        }
    } catch { Say ($label + '=direct:' + $_.Exception.Message) }
    if ($direct) {
        $lt = $null
        try {
            if ($api -eq 'di') { $lt = [Windows.Devices.Enumeration.DeviceInformationCollection] }
            else { $lt = [Windows.Devices.Enumeration.Pnp.PnpObjectCollection] }
        } catch { Say ('listtype=failed:' + $_.Exception.Message) }
        $found = Await $direct $lt
        if ($found) { Say ($label + '=ok count=' + $found.Count); return $found }
        Say ($label + '=await-null')
    }
    $op = $null
    try {
        $callArgs = New-Object 'object[]' 3
        if ($api -eq 'di') {
            $callArgs[0] = ''; $callArgs[1] = $p; $callArgs[2] = $kv
            $op = CallFindAll $diType $callArgs
        } else {
            $callArgs[0] = $kv; $callArgs[1] = $p; $callArgs[2] = ''
            $op = CallFindAll $pnType $callArgs
        }
    } catch { Say ($label + '=invoke:' + $_.Exception.Message); return $null }
    if (-not $op) { Say ($label + '=no-overload'); return $null }
    # The operation's generic argument has to be the collection the method
    # actually returns. AsTask<IReadOnlyList<T>> is not the same interface
    # as IAsyncOperation<DeviceInformationCollection>, so the cast failed
    # on a __ComObject even when the call itself had worked.
    $lt = $null
    try {
        if ($api -eq 'di') { $lt = [Windows.Devices.Enumeration.DeviceInformationCollection] }
        else { $lt = [Windows.Devices.Enumeration.Pnp.PnpObjectCollection] }
    } catch { Say ('listtype=failed:' + $_.Exception.Message) }
    $found = Await $op $lt
    if (-not $found) { Say ($label + '=await-null'); return $null }
    Say ($label + '=ok count=' + $found.Count)
    return $found
}

$aeKv = $null
$aePnp = $null
try { if ($diKind) { $aeKv = [Enum]::Parse($diKind, 'AssociationEndpoint') } } catch { Say ('enum=failed:' + $_.Exception.Message) }
try { if ($pnKind) { $aePnp = [Enum]::Parse($pnKind, 'AssociationEndpoint') } } catch {}

# Four combinations on one scope, so a single run says which API works and
# whether the property list is what is being rejected. Every previous build
# changed one thing and needed another field run to find the next wall.
$found = $null
$using = ''
if ($aeKv) {
    $r = TryCombo 'di+bat' 'di' $aeKv @($BAT)
    if ($r) { $found = $r; $using = 'di+bat' }
    if (-not $found) {
        $r = TryCombo 'di+noprops' 'di' $aeKv @()
        if ($r) { $found = $r; $using = 'di+noprops' }
    }
}
if (-not $found -and $aePnp) {
    $r = TryCombo 'pnp+bat' 'pnp' $aePnp @($BAT)
    if ($r) { $found = $r; $using = 'pnp+bat' }
    if (-not $found) {
        $r = TryCombo 'pnp+noprops' 'pnp' $aePnp @()
        if ($r) { $found = $r; $using = 'pnp+noprops' }
    }
}
Say ('using=' + $using)

# Report every charge found, plus what was seen without one, so an empty
# result says whether Windows sees the headset at all.
$noCharge = @()
$withCharge = 0
if ($found) {
    foreach ($d in $found) {
        $nm = $null
        try { $nm = $d.Name } catch {}
        if (-not $nm) { try { $nm = $d.Properties['System.ItemNameDisplay'] } catch {} }
        $lvl = $null
        try { $lvl = $d.Properties[$BAT] } catch {}
        if ($null -ne $lvl) {
            $withCharge = $withCharge + 1
            Emit (@($nm, $lvl) -join "`t")
        } elseif ($noCharge.Count -lt 25) {
            $noCharge += [string]$nm
        }
    }
}
Say ('withCharge=' + $withCharge)
if ($noCharge.Count -gt 0) { Say ('noCharge=' + ($noCharge -join '; ')) }

