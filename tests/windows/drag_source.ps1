# A WinForms window that starts an OLE file drag (DoDragDrop, FileDrop) when
# the left button goes down in it. Used by tests/windows/drag_probe.py.
param([string]$File, [string]$Title = 'spring-load drag source')
Add-Type -AssemblyName System.Windows.Forms
$script:File = $File
$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.StartPosition = 'Manual'
$form.Left = 100
$form.Top = 100
$form.Width = 400
$form.Height = 300
$form.TopMost = $true
$form.Add_MouseDown({
    $data = New-Object System.Windows.Forms.DataObject
    $files = New-Object System.Collections.Specialized.StringCollection
    [void]$files.Add($script:File)
    $data.SetFileDropList($files)
    $effect = $form.DoDragDrop($data, [System.Windows.Forms.DragDropEffects]::Copy)
    [Console]::Out.WriteLine("drop effect: $effect")
    [Console]::Out.Flush()
})
$form.Add_Shown({ $form.Activate(); [Console]::Out.WriteLine('shown'); [Console]::Out.Flush() })
[System.Windows.Forms.Application]::Run($form)
