# User-approved communication reminder. No file, network or transcript operations.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$eventData = [Console]::In.ReadToEnd() | ConvertFrom-Json
$eventName = [string]$eventData.hook_event_name
if ($eventName -notin @('SessionStart', 'UserPromptSubmit')) {
    [Console]::Out.WriteLine('{}')
    exit 0
}
$reminder = @'
用户已确认的全项目同步沟通要求（2026-10-07）：每轮对话主动判断值得同步的经验、偏好、研究方向或指引变化；出现有价值内容时，及时、充分地向用户说明具体拟同步内容、位置、理由、适用范围及影响，交用户审阅确认，不等用户追问。未获准写回不等于保持沉默。文件修改限于明确授权，所有同步须获用户同意；讨论建议及未确认解释不自动成为持久规则。已批准的具体范围直接落实，新增或实质变化的内容另行沟通；没有有价值的新内容时不制造经验或重复确认。本钩子仅提醒沟通，不保存经验、不批准写入、不改变当前任务或授权。
'@
@{ hookSpecificOutput = @{ hookEventName = $eventName; additionalContext = $reminder } } |
    ConvertTo-Json -Depth 4 -Compress | ForEach-Object { [Console]::Out.WriteLine($_) }
