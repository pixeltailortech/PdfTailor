using System.Diagnostics;
using System.Windows;
using System.Windows.Media;

namespace PdfTailor;

public partial class DoneDialog : Window
{
    private readonly string _path;

    public DoneDialog(string path, int modified, bool verified, string? auditPath = null, string? auditError = null)
    {
        InitializeComponent();
        _path = path;
        PathBox.Text = path;
        if (verified)
        {
            Detail.Text = $"已寫入 {modified} 行修改。檢查通過：修改範圍以外的內容與原檔一致。";
        }
        else
        {
            Heading.Text = "輸出完成（請檢查）";
            Badge.Background = new SolidColorBrush(Color.FromRgb(0xF5, 0xB9, 0x41));
            BadgeText.Text = "!";
            Detail.Text = $"已寫入 {modified} 行修改，但檢查發現修改範圍外也有畫面差異，請開啟檔案確認。";
        }
        ApplyAudit(auditPath, auditError);
    }

    /// <summary>Generic completion box (PDF merge, Word, page organiser).</summary>
    public DoneDialog(string path, string detail, string heading = "合併完成", string? auditPath = null, string? auditError = null)
    {
        InitializeComponent();
        _path = path;
        PathBox.Text = path;
        Heading.Text = heading;
        Detail.Text = detail;
        ApplyAudit(auditPath, auditError);
    }

    /// <summary>The edit record next to the output: where it is, or (yellow) why it could not be written.</summary>
    private void ApplyAudit(string? auditPath, string? auditError)
    {
        if (!string.IsNullOrEmpty(auditPath)) Detail.Text += "\n原始檔未更動。\n修改紀錄：" + auditPath;
        if (!string.IsNullOrEmpty(auditError))
        {
            AuditWarn.Text = "PDF 已輸出，但修改紀錄寫入失敗：" + auditError;
            AuditWarn.Visibility = Visibility.Visible;
        }
    }

    private void OnOpenFile(object sender, RoutedEventArgs e)
    {
        try { Process.Start(new ProcessStartInfo(_path) { UseShellExecute = true }); }
        catch (Exception ex) { MessageBox.Show(this, ex.Message, "PdfTailor"); }
    }

    private void OnOpenFolder(object sender, RoutedEventArgs e)
    {
        try { Process.Start(new ProcessStartInfo("explorer.exe", $"/select,\"{_path}\"") { UseShellExecute = true }); }
        catch (Exception ex) { MessageBox.Show(this, ex.Message, "PdfTailor"); }
    }

    private void OnClose(object sender, RoutedEventArgs e) => Close();
}
