using System.Globalization;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using Shapes = System.Windows.Shapes;

namespace PdfTailor;

// Two additions to the edit view:
//  * 塗改 - draw boxes over damaged or dirty areas; the engine fills them with the paper
//    colour (or white) and rewrites the scanned picture so the old pixels are gone.
//  * jumping straight to a page number instead of stepping page by page.
public partial class MainWindow
{
    private sealed class EraseBox
    {
        public double X, Y, W, H; // display pt, like the text blocks
        public string Mode = "auto";
    }

    private readonly Dictionary<int, List<EraseBox>> _erases = new();
    private EraseBox? _eraseSel;
    private Point? _eraseStart;
    private Shapes.Rectangle? _eraseBand;

    private int EraseCount => _erases.Values.Sum(l => l.Count);

    private bool EraseOn => EraseToggle.IsChecked == true;

    private string EraseFill => (EraseFillBox.SelectedItem as ComboBoxItem)?.Tag as string ?? "auto";

    /// <summary>The boxes in the shape the engine expects; one page only when given.</summary>
    private JsonArray CollectErases(int? onlyPage = null)
    {
        var arr = new JsonArray();
        foreach (var kv in _erases)
        {
            if (onlyPage != null && kv.Key != onlyPage.Value) continue;
            foreach (var b in kv.Value)
            {
                arr.Add(new JsonObject
                {
                    ["page"] = kv.Key,
                    ["rect"] = new JsonArray(b.X, b.Y, b.W, b.H),
                    ["mode"] = b.Mode,
                });
            }
        }
        return arr;
    }

    private void OnEraseToggled(object sender, RoutedEventArgs e)
    {
        bool on = EraseOn;
        EraseLayer.IsHitTestVisible = on;
        EraseLayer.Cursor = on ? Cursors.Cross : null;
        if (on) Select(null);
        else _eraseSel = null;
        DrawErases();
        SetStatus(on
            ? "塗改模式：在頁面上拖曳框出破損或髒汙的區域；點一下方框可選取，按 Delete 移除。取消勾選「框選塗改區域」回到文字編輯。"
            : "已離開塗改模式。");
    }

    private void DrawErases()
    {
        EraseLayer.Children.Clear();
        _eraseBand = null;
        if (!_erases.TryGetValue(_page, out var list)) return;
        double k = DipPerPt;
        foreach (var b in list)
        {
            bool sel = ReferenceEquals(b, _eraseSel);
            var r = new Shapes.Rectangle
            {
                Width = Math.Max(1, b.W * k),
                Height = Math.Max(1, b.H * k),
                Stroke = new SolidColorBrush(sel ? Color.FromRgb(0x28, 0xD7, 0xFF) : Color.FromRgb(0xFF, 0x8A, 0x5B)),
                StrokeThickness = sel ? 2 : 1.2,
                StrokeDashArray = new DoubleCollection { 4, 3 },
                Fill = Brushes.Transparent, // the engine's live picture underneath shows the real fill
                ToolTip = "塗改區域（塗改模式下點選後按 Delete 可移除）",
            };
            Canvas.SetLeft(r, b.X * k);
            Canvas.SetTop(r, b.Y * k);
            var box = b;
            r.MouseLeftButtonDown += (_, ev) =>
            {
                _eraseSel = box;
                foreach (ComboBoxItem item in EraseFillBox.Items)
                    if ((string)item.Tag == box.Mode) { EraseFillBox.SelectedItem = item; break; }
                DrawErases();
                ev.Handled = true;
            };
            EraseLayer.Children.Add(r);
        }
    }

    private void OnEraseDown(object sender, MouseButtonEventArgs e)
    {
        if (!EraseOn || _busy || _path == null) return;
        _eraseSel = null;
        DrawErases();
        _eraseStart = e.GetPosition(EraseLayer);
        _eraseBand = new Shapes.Rectangle
        {
            Stroke = new SolidColorBrush(Color.FromRgb(0x28, 0xD7, 0xFF)),
            StrokeThickness = 1.5,
            Fill = new SolidColorBrush(Color.FromArgb(40, 0x28, 0xD7, 0xFF)),
            IsHitTestVisible = false,
        };
        EraseLayer.Children.Add(_eraseBand);
        EraseLayer.CaptureMouse();
        e.Handled = true;
    }

    private void OnEraseMove(object sender, MouseEventArgs e)
    {
        if (_eraseStart == null || _eraseBand == null) return;
        var a = _eraseStart.Value;
        var p = e.GetPosition(EraseLayer);
        Canvas.SetLeft(_eraseBand, Math.Min(a.X, p.X));
        Canvas.SetTop(_eraseBand, Math.Min(a.Y, p.Y));
        _eraseBand.Width = Math.Abs(p.X - a.X);
        _eraseBand.Height = Math.Abs(p.Y - a.Y);
    }

    private void OnEraseUp(object sender, MouseButtonEventArgs e)
    {
        if (_eraseStart == null) return;
        var a = _eraseStart.Value;
        var p = e.GetPosition(EraseLayer);
        _eraseStart = null;
        EraseLayer.ReleaseMouseCapture();
        double k = DipPerPt;
        double maxX = PageHost.Width, maxY = PageHost.Height;
        double x0 = Math.Clamp(Math.Min(a.X, p.X), 0, maxX), x1 = Math.Clamp(Math.Max(a.X, p.X), 0, maxX);
        double y0 = Math.Clamp(Math.Min(a.Y, p.Y), 0, maxY), y1 = Math.Clamp(Math.Max(a.Y, p.Y), 0, maxY);
        if (x1 - x0 >= 4 && y1 - y0 >= 4) // a click, not a drag, makes no box
        {
            if (!_erases.TryGetValue(_page, out var list)) _erases[_page] = list = new List<EraseBox>();
            var box = new EraseBox { X = x0 / k, Y = y0 / k, W = (x1 - x0) / k, H = (y1 - y0) / k, Mode = EraseFill };
            list.Add(box);
            _eraseSel = box;
            _exported = false;
        }
        DrawErases();
        ErasesChanged();
        e.Handled = true;
    }

    private void ErasesChanged()
    {
        UpdateCounters();
        ScheduleLive();
    }

    private void OnEraseFillChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!IsLoaded || _eraseSel == null) return;
        _eraseSel.Mode = EraseFill;
        ErasesChanged();
    }

    private bool DeleteSelectedErase()
    {
        if (_eraseSel == null) return false;
        foreach (var list in _erases.Values) list.Remove(_eraseSel);
        _eraseSel = null;
        DrawErases();
        ErasesChanged();
        return true;
    }

    private void OnEraseClearPage(object sender, RoutedEventArgs e)
    {
        if (!_erases.TryGetValue(_page, out var list) || list.Count == 0) return;
        list.Clear();
        _eraseSel = null;
        DrawErases();
        ErasesChanged();
    }

    // ---- jump to a page ---------------------------------------------------------------
    private async void OnPageBoxKey(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Escape)
        {
            PageBox.Text = _page.ToString(CultureInfo.InvariantCulture);
            Keyboard.ClearFocus();
            e.Handled = true;
            return;
        }
        if (e.Key != Key.Enter) return;
        e.Handled = true;
        if (_busy || _path == null) return;
        if (int.TryParse(PageBox.Text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int n))
        {
            Keyboard.ClearFocus();
            await GoTo(n); // GoTo clamps to 1..page count
        }
        else
        {
            PageBox.Text = _page.ToString(CultureInfo.InvariantCulture);
            PageBox.SelectAll();
        }
    }

    private void OnPageBoxLostFocus(object sender, RoutedEventArgs e) =>
        PageBox.Text = _path == null ? "" : _page.ToString(CultureInfo.InvariantCulture);

    private void OnPageBoxGotFocus(object sender, RoutedEventArgs e) => PageBox.SelectAll();

    private async void OnFirstPage(object sender, RoutedEventArgs e) { if (!_busy) await GoTo(1); }

    private async void OnLastPage(object sender, RoutedEventArgs e) { if (!_busy) await GoTo(_pageCount); }
}
