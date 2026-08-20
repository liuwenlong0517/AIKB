---
id: aikb:experience:solutions:powershell-profile-psreadline-completion
type: solution
status: verified
tags: [powershell, psreadline, shell, terminal, completion]
applicable_versions: "PowerShell 7.6.4 / PSReadLine 2.4.5 / Windows"
last_verified: 2026-08-05
review_when: "升级 PowerShell 7 或 PSReadLine，或调整 PowerShell profile 中的补全与预测配置时"
supersedes: []
relations: []
---

# PowerShell profile 中实现 Linux 风格列表并区分预测建议与 Tab 补全

## 背景

Windows PowerShell 7 用户希望使用接近 Linux 的 `ll`、`la` 命令，并希望输入命令时的候补提示与 `Tab` 补全行为一致。PowerShell 的默认命令补全和 PSReadLine 的预测建议属于两套不同机制，需要分别配置和使用。

## 问题

原始 `ll` 示例为：

```powershell
function ll { Get-ChildItem -Format List }
```

`Get-ChildItem` 没有 `-Format` 参数，因此调用时会报参数不存在。若确实需要纵向属性列表，应使用 `Get-ChildItem | Format-List`，但 Linux 风格的 `ll` 更适合直接使用 `Get-ChildItem` 的默认表格输出。

另外，PSReadLine 默认将 `Tab` 绑定到 `TabCompleteNext`，其语义是选择下一个补全候选，并不是接受当前显示的灰色预测建议。因此用户看到的行内建议与按 `Tab` 后得到的候选可能不同。

## 解决方案

### 文件列表函数

使用参数透传的函数实现 `ll` 和 `la`：

```powershell
function ll { Get-ChildItem @args }
function la { Get-ChildItem -Force @args }
Set-Alias -Name l -Value ll -Force
```

这样既支持 `ll .\src`，也支持 `ll -Directory` 等 `Get-ChildItem` 参数；`la` 额外包含隐藏项。

### PSReadLine 配置

在交互式终端中启用本地历史预测，并将 `Tab` 改为打开候选菜单：

```powershell
if (Get-Command Set-PSReadLineOption -ErrorAction SilentlyContinue) {
    if (-not [Console]::IsOutputRedirected) {
        try {
            Set-PSReadLineOption -PredictionSource History -PredictionViewStyle InlineView -ErrorAction Stop
        } catch {
            # 不支持虚拟终端时跳过预测配置。
        }
    }

    Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete -ErrorAction SilentlyContinue
}
```

交互约定如下：

- 灰色行内预测建议：使用 `RightArrow` 接受。
- 命令或参数补全：使用 `Tab` 打开候选菜单。
- 候选菜单：使用 `UpArrow`、`DownArrow` 选择，使用 `Enter` 确认，使用 `Escape` 取消。

### 其他低风险快捷操作

可以在同一 profile 中增加 `l`、`c`、`h`、`grep`、`open`、`which`、`touch`、`mkcd` 和 `reload-profile`，但应优先使用函数透传参数，不要把需要参数的命令简单替换成不能处理参数的别名。

## 验证

在 Windows PowerShell 7.6.4、PSReadLine 2.4.5 环境中完成验证：

- profile 脚本语法检查通过。
- 交互式 PowerShell 进程显示 `PredictionSource=History`、`PredictionViewStyle=InlineView`、`Tab=MenuComplete`。
- `ll`、`la` 均成功加载；`la` 比 `ll` 多包含隐藏项。
- `ll -Directory`、路径参数和 `la` 的隐藏项行为正常。
- `touch`、`mkcd`、`grep`、`which` 的实际调用通过。
- 输出被重定向时跳过预测设置，不再因为虚拟终端不可用而产生配置错误。

## 适用范围

适用于 Windows PowerShell 7 与 PSReadLine 2.2 及以上版本。预测功能依赖交互式、支持虚拟终端的宿主；脚本、管道和重定向输出场景不应强制启用行内预测。`PredictionSource=History` 使用本地 PSReadLine 历史记录，不等同于命令参数补全，也不会替换具体命令的 argument completer。

如果用户明确希望 `Tab` 直接接受灰色预测，而不是打开命令补全菜单，需要单独设计按键映射；不要在未确认取舍前用单一按键同时覆盖两种行为。

## 关联信息

- [Set-PSReadLineOption](https://learn.microsoft.com/en-us/powershell/module/psreadline/set-psreadlineoption?view=powershell-7.6)：预测来源与显示模式。
- [Get-PSReadLineKeyHandler](https://learn.microsoft.com/en-us/powershell/module/psreadline/get-psreadlinekeyhandler?view=powershell-7.5)：默认按键绑定及 `TabCompleteNext`、`MenuComplete` 语义。
- [Using predictors in PSReadLine](https://learn.microsoft.com/en-gb/powershell/scripting/learn/shell/using-predictors?view=powershell-7.4)：PSReadLine 预测建议使用说明。
