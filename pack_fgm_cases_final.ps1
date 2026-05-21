# =====================================================================
# FGM 工况目录正式打包脚本
# =====================================================================
#
# 这个脚本用于把服务器上的大量小文件打包成 tar 文件。
#
# 你的原始数据结构大概是：
#
#   E:\FGM\run
#   ├─ initial_mixing_line_no_reactions
#   │  ├─ 某个 csv 日志
#   │  ├─ T=1000 K_p=100 bar_a=1 s-1
#   │  │  ├─ config
#   │  │  ├─ out.h5
#   │  │  ├─ prof000000.h5
#   │  │  └─ ...
#   │  └─ ...
#   └─ igniting_from_mixing_line
#      ├─ 某个 csv 日志
#      ├─ T=400 K_p=20 bar_a=366.802 s-1
#      │  ├─ config
#      │  ├─ out.h5
#      │  ├─ prof000000.h5
#      │  └─ ...
#      └─ ...
#
# 脚本输出结构大概是：
#
#   F:\FGM_tar
#   ├─ pack_fgm_cases.log
#   ├─ initial_mixing_line_no_reactions
#   │  ├─ _root_files.tar
#   │  ├─ T=1000 K_p=100 bar_a=1 s-1.tar
#   │  └─ ...
#   └─ igniting_from_mixing_line
#      ├─ _root_files.tar
#      ├─ T=400 K_p=20 bar_a=366.802 s-1.tar
#      └─ ...
#
# 为什么这样做：
#   FileZilla 直接传几百万个小 .h5 文件会非常慢。
#   打成 tar 后，FileZilla 只需要传几千个较大的 tar 文件，效率高很多。
#
# 这个脚本不会做的事情：
#   1. 不删除任何原始数据。
#   2. 不删除已经生成好的 .tar 文件。
#   3. 不上传、不下载，只在服务器本地打包。
#   4. 不压缩，只打包，所以不会大量占用 CPU。
#
# 使用方式：
#   在服务器 PowerShell 里进入脚本所在目录，然后执行：
#
#     powershell -ExecutionPolicy Bypass -File .\pack_fgm_cases_final.ps1
#
# =====================================================================

# 遇到错误就停止当前脚本。
# 默认 PowerShell 有些错误只是打印一下然后继续跑；
# 这里设成 Stop，可以避免“前面已经错了，后面还继续生成一堆异常结果”。
$ErrorActionPreference = "Stop"

# =====================================================================
# 基本配置
# =====================================================================

# 原始数据根目录。
# 脚本会在这个目录下面寻找两个分组目录：
#   initial_mixing_line_no_reactions
#   igniting_from_mixing_line
$SourceRoot = "E:\FGM\run"

# tar 包输出目录。
# 脚本会自动创建这个目录。
# 建议输出目录不要放在原始目录里面，避免后续扫描时混在一起。
$OutputRoot = "F:\FGM_tar"

# 并行打包数量。
# 根据前面的实测，32 并行大约能把服务器磁盘吞吐跑满。
# 如果服务器变得很卡，可以改成 16。
# 如果你以后换了更强的磁盘，可以再测试 64。
$MaxParallelJobs = 32

# 心跳输出间隔，单位秒。
# 脚本会每隔这么久打印一次当前正在运行的任务状态。
# 这样你能看到：
#   哪些 case 正在打包
#   已经跑了多久
#   当前 .partial 文件已经写到多大
#   这个 case 当前平均速度大概是多少
$HeartbeatSeconds = 30

# 需要处理的两个分组目录名。
# 这两个名字会和 $SourceRoot 拼起来，形成完整路径：
#   E:\FGM\run\initial_mixing_line_no_reactions
#   E:\FGM\run\igniting_from_mixing_line
$Groups = @(
    "initial_mixing_line_no_reactions",
    "igniting_from_mixing_line"
)

# 日志文件。
# 所有“开始、跳过、完成、错误”等信息都会写入这里。
$LogFile = Join-Path $OutputRoot "pack_fgm_cases.log"

# =====================================================================
# 函数：Write-Log
# =====================================================================
#
# 作用：
#   同时把一条消息打印到屏幕，并追加写入日志文件。
#
# 为什么写成函数：
#   后面很多地方都需要写日志。
#   用函数可以保证日志格式统一，也减少重复代码。
#
# PowerShell 语法说明：
#   function Write-Log { ... } 定义一个函数。
#   param(...) 定义函数参数。
#   [string]$Message 表示 Message 参数应该是字符串。
#   Add-Content 表示往文件末尾追加内容。
#
function Write-Log {
    param(
        [string]$Message
    )

    # 生成当前时间字符串，例如：2026-05-21 18:00:00。
    $time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

    # 拼出最终日志行。
    # -f 是 PowerShell 的格式化字符串写法：
    #   "{0} {1}" -f "A","B"  会变成  "A B"
    $line = "[{0}] {1}" -f $time, $Message

    # 打印到当前 PowerShell 窗口。
    Write-Host $line

    # 追加写入日志文件。
    # -LiteralPath 表示路径按字面量处理，路径里有特殊字符也不乱解析。
    # -Encoding UTF8 表示用 UTF-8 写日志。
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

# =====================================================================
# 函数：Update-JobStatus
# =====================================================================
#
# 作用：
#   更新所有后台任务的状态。
#
# 背景：
#   这个脚本用 Start-Job 启动后台打包任务。
#   Start-Job 只是把任务放到后台，主脚本需要定期看这些任务的状态。
#
# 这个函数做两件事：
#   1. 找出已经结束的 Job，读取 DONE 或错误信息，写入日志，然后清理 Job 记录。
#   2. 按心跳间隔打印仍在运行的 Job 状态，也就是 RUNNING 信息。
#
function Update-JobStatus {
    param(
        # 如果传入 -ForceHeartbeat，就不管时间间隔是否到了，都打印一次 RUNNING 状态。
        [switch]$ForceHeartbeat
    )

    # 当前时间。后面计算 elapsed 和心跳间隔都要用。
    $now = Get-Date

    # -------------------------------------------------------------
    # 第一部分：处理已经结束的 Job
    # -------------------------------------------------------------
    #
    # Get-Job 会列出当前 PowerShell 会话中的后台任务。
    # 这里筛选出已经结束的任务：
    #   Completed = 正常完成
    #   Failed    = 失败
    #   Stopped   = 被停止
    Get-Job | Where-Object { $_.State -in @("Completed", "Failed", "Stopped") } | ForEach-Object {
        # $_ 表示当前管道传进来的对象。
        # 这里把它保存成 $job，后面读起来更清楚。
        $job = $_

        # 从“运行中任务表”里找到这个 Job 对应的 case 信息。
        # 运行中任务表是在 Start-Job 之后由主脚本维护的。
        $info = $script:ActiveJobs[$job.Id]

        try {
            # Receive-Job 会取出后台任务输出的内容。
            # 例如后面 ScriptBlock 里输出的 "DONE ..."
            Receive-Job -Job $job -ErrorAction Continue | ForEach-Object {
                if ($null -ne $_) {
                    Write-Log "$_"
                }
            }

            # 如果 Job 不是 Completed，就说明它失败或被停止了。
            # 这种情况必须写日志，否则你可能只看到任务消失，但不知道为什么。
            if ($job.State -ne "Completed") {
                if ($null -ne $info) {
                    Write-Log "ERROR $($info.Index)/$($info.Total) $($info.DisplayName) | JobId=$($job.Id) | State=$($job.State)"
                }
                else {
                    Write-Log "ERROR JobId=$($job.Id) | State=$($job.State)"
                }

                # 一个 Job 里面可能有 ChildJobs。
                # 错误信息通常在 ChildJobs 的 Error 集合里。
                foreach ($child in $job.ChildJobs) {
                    foreach ($err in $child.Error) {
                        Write-Log "JOB $($job.Id) 错误：$err"
                    }
                }
            }
        }
        finally {
            # 不管成功还是失败，都把这个 Job 从当前会话移除。
            # 这不会删除数据，只是清理 PowerShell 后台任务记录。
            Remove-Job -Job $job

            # 同时从脚本自己的“运行中任务表”里移除。
            # 这也不会删除数据，只是表示这个 case 已经不再运行。
            if ($script:ActiveJobs.ContainsKey($job.Id)) {
                $script:ActiveJobs.Remove($job.Id)
            }
        }
    }

    # -------------------------------------------------------------
    # 第二部分：打印 RUNNING 心跳
    # -------------------------------------------------------------
    #
    # 如果到了心跳时间，或者调用函数时强制要求打印心跳，
    # 就把当前还在运行的 case 状态写入日志。
    $elapsedSinceHeartbeat = ($now - $script:LastHeartbeatTime).TotalSeconds
    if ((!$ForceHeartbeat) -and ($elapsedSinceHeartbeat -lt $HeartbeatSeconds)) {
        return
    }

    # 更新时间戳，避免下一秒又重复打印心跳。
    $script:LastHeartbeatTime = $now

    # 取出当前正在运行的任务信息，并按照任务编号排序，日志更容易看。
    $runningInfos = @($script:ActiveJobs.Values | Sort-Object Index)

    if ($runningInfos.Count -eq 0) {
        return
    }

    Write-Log "HEARTBEAT running=$($runningInfos.Count)"

    foreach ($info in $runningInfos) {
        # 计算这个 case 已经运行了多久。
        $elapsedSeconds = [math]::Max(1, ($now - $info.StartTime).TotalSeconds)

        # .partial 文件大小。
        # 如果 tar.exe 已经创建并正在写入，这里能看到它变大。
        # 如果刚启动还没创建文件，则大小记为 0。
        $partialBytes = 0L
        if (Test-Path -LiteralPath $info.PartialTar) {
            $partialBytes = (Get-Item -LiteralPath $info.PartialTar).Length
        }

        $partialGB = [math]::Round($partialBytes / 1GB, 3)
        $avgMBps = [math]::Round(($partialBytes / $elapsedSeconds) / 1MB, 2)

        # TimeSpan 用来把秒数显示成 00:01:30 这种形式。
        $elapsedText = ([TimeSpan]::FromSeconds($elapsedSeconds)).ToString("hh\:mm\:ss")

        Write-Log "RUNNING $($info.Index)/$($info.Total) $($info.DisplayName) | elapsed=$elapsedText | partial=$partialGB GB | avg=$avgMBps MB/s"
    }
}

# =====================================================================
# 初始化检查
# =====================================================================

# 创建输出目录。
# -Force 表示目录已存在也不报错。
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

# 如果日志文件不存在，就先创建一个空日志文件。
if (!(Test-Path -LiteralPath $LogFile)) {
    New-Item -ItemType File -Force -Path $LogFile | Out-Null
}

# 检查原始数据根目录是否存在。
# 如果这里不存在，说明 $SourceRoot 写错了，直接停止。
if (!(Test-Path -LiteralPath $SourceRoot)) {
    throw "源目录不存在：$SourceRoot"
}

# 检查 Windows 是否能找到 tar.exe。
# Windows 10/11 通常自带 tar.exe。
# tar.exe 是真正负责打包的程序。
if ($null -eq (Get-Command tar.exe -ErrorAction SilentlyContinue)) {
    throw "没有找到 tar.exe"
}

Write-Log "开始正式打包"
Write-Log "源目录：$SourceRoot"
Write-Log "输出目录：$OutputRoot"
Write-Log "并行任务数：$MaxParallelJobs"
Write-Log "心跳间隔：$HeartbeatSeconds 秒"

# =====================================================================
# 收集打包任务
# =====================================================================
#
# 这里不会立刻打包。
# 这里只是先把“需要打包什么”整理成一个任务列表。
#
# 每个任务包含：
#   SourceParent：要打包对象所在的父目录。
#   ItemNames：要打包的目录名或文件名。
#   OutputTar：输出 tar 路径。
#   DisplayName：日志里显示的名字。
#

# 创建一个列表，用来保存所有打包任务。
$Tasks = New-Object System.Collections.Generic.List[object]

foreach ($group in $Groups) {
    # 当前分组的源目录，例如：
    #   E:\FGM\run\igniting_from_mixing_line
    $groupSource = Join-Path $SourceRoot $group

    # 当前分组的输出目录，例如：
    #   F:\FGM_tar\igniting_from_mixing_line
    $groupOutput = Join-Path $OutputRoot $group

    # 如果分组目录不存在，直接停止。
    if (!(Test-Path -LiteralPath $groupSource)) {
        throw "分组目录不存在：$groupSource"
    }

    # 创建分组输出目录。
    New-Item -ItemType Directory -Force -Path $groupOutput | Out-Null

    Write-Log "扫描分组：$group"

    # 处理分组根目录下的普通文件。
    # 例如 csv 日志文件通常就在：
    #   E:\FGM\run\igniting_from_mixing_line\xxx.csv
    #
    # 这些文件不属于任何一个工况目录，所以单独打成 _root_files.tar。
    $rootFiles = Get-ChildItem -LiteralPath $groupSource -File
    if ($rootFiles.Count -gt 0) {
        $Tasks.Add([pscustomobject]@{
            SourceParent = $groupSource
            ItemNames = @($rootFiles | ForEach-Object { $_.Name })
            OutputTar = Join-Path $groupOutput "_root_files.tar"
            DisplayName = "$group 根目录文件"
        })
    }

    # 处理每一个工况目录。
    # 每个工况目录生成一个 tar 包。
    #
    # 例如：
    #   E:\FGM\run\igniting_from_mixing_line\T=400 K_p=20 bar_a=366.802 s-1
    #
    # 会生成：
    #   F:\FGM_tar\igniting_from_mixing_line\T=400 K_p=20 bar_a=366.802 s-1.tar
    Get-ChildItem -LiteralPath $groupSource -Directory | ForEach-Object {
        $caseName = $_.Name

        $Tasks.Add([pscustomobject]@{
            SourceParent = $groupSource
            ItemNames = @($caseName)
            OutputTar = Join-Path $groupOutput ($caseName + ".tar")
            DisplayName = "$group\$caseName"
        })
    }
}

$total = $Tasks.Count
Write-Log "共收集到 $total 个任务"

# =====================================================================
# 执行打包任务
# =====================================================================
#
# 主循环负责不断提交后台任务。
# 后台任务真正执行 tar.exe。
#
# 为什么用后台任务：
#   单个 tar.exe 只能打一个工况目录。
#   用 Start-Job 可以同时打多个工况目录。
#   根据测试，32 个并行任务比较合适。
#

$index = 0

# 运行中任务表。
#
# 这个表由主脚本维护，key 是 JobId，value 是这个 Job 对应的 case 信息。
# 有了这个表，脚本才能在心跳里打印：
#   RUNNING 123/2378 某个 case | elapsed=... | partial=... GB
#
# 注意：
#   这个表只是内存里的状态表。
#   它不会写入原始数据，也不会影响 tar 文件。
$script:ActiveJobs = @{}

# 上一次打印心跳的时间。
# 这里先设为当前时间，表示脚本启动后先不用立刻打印空心跳。
$script:LastHeartbeatTime = Get-Date

foreach ($task in $Tasks) {
    $index++

    # 每处理一个新任务前，先更新一次后台任务状态。
    # 这样已经完成的任务会尽快打印 DONE，
    # 正在运行的任务也会按心跳间隔打印 RUNNING。
    Update-JobStatus

    # 如果正式 .tar 已经存在，并且大小大于 0，
    # 说明这个工况之前已经打包完成，本次直接跳过。
    #
    # 这让脚本具备“断点继续”的能力：
    #   脚本中断后再次运行，不会重新打已经完成的 tar。
    if ((Test-Path -LiteralPath $task.OutputTar) -and ((Get-Item -LiteralPath $task.OutputTar).Length -gt 0)) {
        Write-Log "跳过已存在：$index/$total $($task.DisplayName)"
        continue
    }

    # 正在打包时，先输出为 .partial 文件。
    # 只有 tar.exe 完整成功后，才改名成正式 .tar。
    #
    # 这样做的好处：
    #   如果打包中途断电或脚本中断，不会留下看起来完整但实际损坏的 .tar。
    $partialTar = "$($task.OutputTar).partial"

    # 如果上次中断留下了 .partial，先删除这个半成品。
    # 注意：这里只删除 .partial，不删除正式 .tar，也不删除原始数据。
    if (Test-Path -LiteralPath $partialTar) {
        Remove-Item -LiteralPath $partialTar -Force
    }

    # 控制并行数量。
    #
    # 如果当前正在运行的 Job 数量已经达到 $MaxParallelJobs，
    # 主循环就暂停一下，等待某些任务完成。
    while ((Get-Job -State Running).Count -ge $MaxParallelJobs) {
        Update-JobStatus
        Start-Sleep -Seconds 2
    }

    # Start-Job 会启动一个后台任务。
    #
    # -ArgumentList 后面的变量会传给 ScriptBlock 里的 param(...)。
    #
    # ScriptBlock 里面才是真正执行 tar.exe 的地方。
    $job = Start-Job -ArgumentList `
        $task.SourceParent, `
        $task.ItemNames, `
        $task.OutputTar, `
        $partialTar, `
        $task.DisplayName, `
        $index, `
        $total `
        -ScriptBlock {
            param(
                [string]$SourceParent,
                [string[]]$ItemNames,
                [string]$OutputTar,
                [string]$PartialTar,
                [string]$DisplayName,
                [int]$Index,
                [int]$Total
            )

            # 记录任务开始时间，用来计算这个工况的平均打包速度。
            $start = Get-Date

            # 真正执行打包。
            #
            # tar.exe 参数解释：
            #
            #   -C $SourceParent
            #       先切换到这个父目录。
            #       这样 tar 包里保存的是相对路径，而不是 E:\FGM\run\... 这种绝对路径。
            #
            #   -cf $PartialTar
            #       c = create，创建 tar 包。
            #       f = file，后面跟输出文件路径。
            #       这里输出到 .partial 半成品文件。
            #
            #   @ItemNames
            #       PowerShell 的数组展开写法。
            #       如果 ItemNames 里有一个工况目录名，就打包这个目录。
            #       如果 ItemNames 里有多个 csv/log 文件名，就一起打到 _root_files.tar。
            & tar.exe -C $SourceParent -cf $PartialTar @ItemNames

            # tar.exe 结束后，$LASTEXITCODE 保存它的退出码。
            # 0 表示成功，非 0 表示失败。
            if ($LASTEXITCODE -ne 0) {
                throw "tar.exe 打包失败：$DisplayName，退出码：$LASTEXITCODE"
            }

            # 只有 tar.exe 成功后，才把 .partial 改名为正式 .tar。
            Move-Item -LiteralPath $PartialTar -Destination $OutputTar -Force

            # 计算打包耗时、输出文件大小、平均速度。
            $seconds = [math]::Max(1, ((Get-Date) - $start).TotalSeconds)
            $sizeBytes = (Get-Item -LiteralPath $OutputTar).Length
            $sizeGB = [math]::Round($sizeBytes / 1GB, 3)
            $speedMBps = [math]::Round(($sizeBytes / $seconds) / 1MB, 2)

            # 这行字符串会被 Update-JobStatus 取出来并写入日志。
            "DONE  $Index/$Total $DisplayName | $sizeGB GB | $speedMBps MB/s"
        }

    # 把这个 Job 加入“运行中任务表”。
    # 之后 Update-JobStatus 就能根据 JobId 找到它对应的 case 信息。
    $script:ActiveJobs[$job.Id] = [pscustomobject]@{
        JobId = $job.Id
        Index = $index
        Total = $total
        DisplayName = $task.DisplayName
        PartialTar = $partialTar
        OutputTar = $task.OutputTar
        StartTime = Get-Date
    }

    # 现在这个 case 已经交给后台 Job 运行了。
    # 用 START 比“提交任务”更直观：它表示这个 case 已经进入运行队列。
    Write-Log "START $index/$total $($task.DisplayName) | JobId=$($job.Id)"
}

# 所有任务都已经提交后，还要等待剩下的后台任务完成。
while ($script:ActiveJobs.Count -gt 0) {
    Update-JobStatus
    Start-Sleep -Seconds 2
}

# 最后强制刷新一次状态，避免最后一批 DONE 还没显示。
Update-JobStatus -ForceHeartbeat

Write-Log "所有打包任务结束"
Write-Log "输出目录：$OutputRoot"
