using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Media;
using System.Windows.Threading;
using PdfTailor.Engine;

namespace PdfTailor;

// Live view of edits: a moment after a line changes, the engine renders the page exactly
// as it will be exported and that picture is shown under the overlay. Until it arrives
// the changed line is drawn by WPF in the nearest installed font, which is only a
// stand-in (it cannot show the scan's own pixels or the engine's font choice).
public partial class MainWindow
{
    // ---- OCR quality ------------------------------------------------------------------
    // Off (default): small models read everything, the medium model checks the lines they
    // are unsure of. On: every line goes through the medium models - several times slower.
    private bool _maxQuality;

    private string OcrQuality => _maxQuality ? "max" : "auto";

    private void OnQualityToggled(object sender, RoutedEventArgs e)
    {
        _maxQuality = (sender as System.Windows.Controls.CheckBox)?.IsChecked == true;
        QualityBox.IsChecked = _maxQuality;
        WordQualityBox.IsChecked = _maxQuality;
        SetStatus(_maxQuality
            ? "最高精度辨識：已開啟。之後辨識的頁面每一行都用大模型（較慢）；已辨識過的頁面請按「重新辨識此頁」。"
            : "最高精度辨識：已關閉。輕量模型全讀，沒把握的行才交給大模型複核。");
    }

    // ---- live view of edits -------------------------------------------------------------
    private DispatcherTimer? _liveTimer;
    private int _liveSerial;
    private readonly Dictionary<BlockVm, string> _liveShown = new();

    private static string LiveKey(BlockVm b) => b.ToEditJson().ToJsonString();

    /// <summary>True when the engine's picture on screen already shows this block as it is now.</summary>
    private bool LiveShows(BlockVm b) => _liveShown.TryGetValue(b, out var key) && key == LiveKey(b);

    private void ScheduleLive()
    {
        if (_liveTimer == null)
        {
            var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(450) };
            timer.Tick += async (_, _) =>
            {
                timer.Stop();
                await UpdateLive();
            };
            _liveTimer = timer;
        }
        _liveTimer.Stop();
        _liveTimer.Start();
    }

    private void ClearLive()
    {
        _liveSerial++;
        _liveShown.Clear();
        LiveImage.Source = null;
        LiveImage.Visibility = Visibility.Collapsed;
    }

    private async Task UpdateLive()
    {
        if (_path == null || !_blocks.TryGetValue(_page, out var list))
        {
            ClearLive();
            return;
        }
        if (_busy)
        {
            ScheduleLive(); // try again once the current job is done
            return;
        }
        var modified = list.Where(b => b.Modified).ToList();
        var pageErases = CollectErases(_page);
        if (modified.Count == 0 && pageErases.Count == 0)
        {
            ClearLive();
            foreach (var b in list) RefreshVisual(b);
            return;
        }

        int serial = ++_liveSerial;
        int page = _page;
        var keys = new Dictionary<BlockVm, string>();
        foreach (var b in modified) keys[b] = LiveKey(b);
        var edits = new JsonArray();
        foreach (var b in list) edits.Add(b.ToEditJson());
        double scale = VisualTreeHelper.GetDpi(this).DpiScaleX;
        double dpi = Math.Min(300, Math.Round(96 * _zoom * scale));
        try
        {
            var args = new JsonObject { ["edits"] = edits, ["erases"] = pageErases, ["pages"] = new JsonArray(page), ["dpi"] = dpi };
            var pv = (await _engine.CallAsync("preview", args))?.AsObject();
            if (pv == null || serial != _liveSerial || page != _page) return; // a newer edit is on its way
            string? file = null;
            foreach (var img in pv["images"]!.AsArray())
                if (img!["page"]!.GetValue<int>() == page) file = img["path"]!.GetValue<string>();
            if (file == null) return;
            LiveImage.Source = LoadBitmap(file);
            LiveImage.Visibility = Visibility.Visible;
            _liveShown.Clear();
            foreach (var kv in keys) _liveShown[kv.Key] = kv.Value;
            foreach (var b in list) RefreshVisual(b);
        }
        catch (Exception)
        {
            // the stand-in drawing stays; the preview window still shows the real result
        }
    }
}
