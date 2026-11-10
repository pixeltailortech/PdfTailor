using System.Collections.ObjectModel;
using System.Collections.Specialized;
using System.IO;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using Microsoft.Win32;
using PdfTailor.Services;
using PageRange = PdfTailor.Services.PageRange;  // System.Windows.Controls has one too

namespace PdfTailor;

// "整理頁面" page: rotate, delete, reorder and split the pages of the open PDF. The engine does the
// work on the ORIGINAL file (it does not know about text edits that have not been exported yet).
public partial class MainWindow
{
    private readonly ObservableCollection<PageItem> _pages = new();
    private readonly List<(int Src, string Path, double W, double H)> _thumbCache = new();
    private string? _pagesFor;          // the file the thumbnails belong to
    private bool _rebuildingPages;
    private List<PageItem>? _dragPages;
    private Point _pagesDragStart;

    private void InitPages()
    {
        PagesList.ItemsSource = _pages;
        _pages.CollectionChanged += OnPagesChanged;
        PagesSplitEvery.IsChecked = true;
        UpdatePagesSplitUi();
        UpdatePagesView();
    }

    // ================================================================ state
    private void OnPagesChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (_rebuildingPages) return;
        for (int i = 0; i < _pages.Count; i++) _pages[i].Order = i + 1;
        UpdatePagesView();
    }

    private void UpdatePagesView()
    {
        if (PagesSplitEveryBox == null) return;   // still loading the XAML
        bool has = _path != null;
        PagesFileName.Text = has ? Path.GetFileName(_path) : "尚未開啟 PDF";
        PagesFileInfo.Text = has ? $"原檔共 {_pageCount} 頁" : "把 PDF 拖進來，或按「開啟 PDF」。";

        bool any = _pages.Count > 0;
        PagesEmptyHint.Visibility = any ? Visibility.Collapsed : Visibility.Visible;
        PagesEmptyHint.Text = !has ? "尚未開啟 PDF。" : _busy ? "縮圖載入中…" : "縮圖尚未載入；按「重設」重新載入。";
        int selected = PagesList.SelectedItems.Count;
        int deleted = has ? Math.Max(0, _pageCount - _pages.Count) : 0;
        PagesSummary.Text = !any ? "—" : $"輸出 {_pages.Count} 頁" + (deleted > 0 ? $"（已刪除 {deleted} 頁）" : "") + (selected > 0 ? $" · 已選 {selected} 頁" : "");
        PagesExportButton.IsEnabled = any;
        PagesSplitButton.IsEnabled = any;
    }

    private void OnPagesSelectionChanged(object sender, SelectionChangedEventArgs e) => UpdatePagesView();

    private void OnPagesOpen(object sender, RoutedEventArgs e) => OnOpen(sender, e);

    /// <summary>Builds the thumbnail list for the open PDF (once per file).</summary>
    private async Task LoadPagesAsync(bool force = false)
    {
        if (_path == null)
        {
            _pages.Clear(); _thumbCache.Clear(); _pagesFor = null;
            UpdatePagesView();
            return;
        }
        if (!force && _pagesFor == _path && _thumbCache.Count > 0) return;

        JsonObject? result = null;
        await Run("載入縮圖中…", async ct =>
        {
            result = (await _engine.CallAsync("thumbnails", new JsonObject { ["dpi"] = 40 }, ct))!.AsObject();
        }, showBeam: false);
        if (result == null) { UpdatePagesView(); return; }

        try
        {
            _thumbCache.Clear();
            foreach (var im in result["images"]!.AsArray())
                _thumbCache.Add((im!["page"]!.GetValue<int>(), im["path"]!.GetValue<string>(), im["width"]!.GetValue<double>(), im["height"]!.GetValue<double>()));
            _pagesFor = _path;
            RebuildPages();
            SetStatus("選取頁面後可旋轉、刪除，或拖曳調整順序。");
        }
        catch (Exception ex)
        {
            _pagesFor = null;
            SetStatus("縮圖載入失敗：" + ex.Message);
        }
        UpdatePagesView();
    }

    /// <summary>Back to the original state: every page, original order, no rotation.</summary>
    private void RebuildPages()
    {
        _rebuildingPages = true;
        try
        {
            _pages.Clear();
            foreach (var t in _thumbCache) _pages.Add(new PageItem(t.Src, LoadBitmap(t.Path), t.W, t.H));
        }
        finally { _rebuildingPages = false; }
        for (int i = 0; i < _pages.Count; i++) _pages[i].Order = i + 1;
        UpdatePagesView();
    }

    private async void OnPagesReset(object sender, RoutedEventArgs e)
    {
        if (_busy || _path == null) return;
        if (_thumbCache.Count > 0 && _pagesFor == _path) { RebuildPages(); SetStatus("已重設：回到原始頁面與順序。"); }
        else await LoadPagesAsync(force: true);
    }

    // ================================================================ rotate / delete / select
    private List<PageItem> SelectedPages() => PagesList.SelectedItems.Cast<PageItem>().ToList();

    private void OnPagesRotate(object sender, RoutedEventArgs e)
    {
        if (_busy || sender is not Button { Tag: string dir }) return;
        var sel = SelectedPages();
        if (sel.Count == 0) { SetStatus("先選取要旋轉的頁面（Ctrl／Shift 可多選）。"); return; }
        int delta = dir == "left" ? -90 : 90;
        foreach (var p in sel) p.Rotation += delta;
        SetStatus($"已將 {sel.Count} 頁{(delta < 0 ? "向左" : "向右")}轉 90°。");
    }

    private void OnPagesDelete(object sender, RoutedEventArgs e) => DeleteSelectedPages();

    private void DeleteSelectedPages()
    {
        if (_busy) return;
        var sel = SelectedPages();
        if (sel.Count == 0) { SetStatus("先選取要刪除的頁面。"); return; }
        if (sel.Count >= _pages.Count) { SetStatus("至少要保留 1 頁。"); return; }
        foreach (var p in sel) _pages.Remove(p);
        SetStatus($"已刪除 {sel.Count} 頁（輸出時才會真正寫成新檔；原檔不會更動）。");
    }

    private void OnPagesSelectAll(object sender, RoutedEventArgs e)
    {
        if (!_busy) PagesList.SelectAll();
    }

    // ================================================================ drag to reorder
    private void OnPagesMouseDown(object sender, MouseButtonEventArgs e)
    {
        _pagesDragStart = e.GetPosition(PagesList);
        var item = FindParent<ListViewItem>(e.OriginalSource as DependencyObject)?.DataContext as PageItem;
        if (item == null) { _dragPages = null; return; }
        // Taken before the click changes the selection: dragging one of several selected
        // pages moves all of them.
        var sel = SelectedPages();
        _dragPages = sel.Contains(item) ? sel : new List<PageItem> { item };
    }

    private void OnPagesMouseMove(object sender, MouseEventArgs e)
    {
        if (_busy || _dragPages == null || e.LeftButton != MouseButtonState.Pressed) return;
        var p = e.GetPosition(PagesList);
        if (Math.Abs(p.X - _pagesDragStart.X) < SystemParameters.MinimumHorizontalDragDistance &&
            Math.Abs(p.Y - _pagesDragStart.Y) < SystemParameters.MinimumVerticalDragDistance) return;
        var moving = _dragPages;
        _dragPages = null;
        DragDrop.DoDragDrop(PagesList, new DataObject(typeof(List<PageItem>), moving), DragDropEffects.Move);
    }

    private void OnPagesDragOver(object sender, DragEventArgs e)
    {
        if (_busy) { e.Effects = DragDropEffects.None; e.Handled = true; return; }
        if (e.Data.GetDataPresent(typeof(List<PageItem>))) { e.Effects = DragDropEffects.Move; e.Handled = true; }
        // files from Explorer are left to the window, which opens the PDF
    }

    private void OnPagesDrop(object sender, DragEventArgs e)
    {
        if (_busy || e.Data.GetData(typeof(List<PageItem>)) is not List<PageItem> moving) return;
        e.Handled = true;
        var row = FindParent<ListViewItem>(e.OriginalSource as DependencyObject);
        int at = row?.DataContext is PageItem target ? _pages.IndexOf(target) : _pages.Count;
        if (row != null && e.GetPosition(row).X > row.ActualWidth / 2) at++;
        MovePages(moving, at);
    }

    /// <summary>Moves <paramref name="moving"/> (keeping their relative order) so they sit in front of
    /// the item that is at index <paramref name="before"/> of the current list.</summary>
    private void MovePages(List<PageItem> moving, int before)
    {
        var set = new HashSet<PageItem>(moving);
        int shift = _pages.Take(Math.Clamp(before, 0, _pages.Count)).Count(set.Contains);
        var ordered = _pages.Where(set.Contains).ToList();
        if (ordered.Count == 0) return;
        foreach (var p in ordered) _pages.Remove(p);
        int to = Math.Clamp(before - shift, 0, _pages.Count);
        for (int k = 0; k < ordered.Count; k++) _pages.Insert(to + k, ordered[k]);
        PagesList.SelectedItems.Clear();
        foreach (var p in ordered) PagesList.SelectedItems.Add(p);
        SetStatus($"已移動 {ordered.Count} 頁到第 {to + 1} 位。");
    }

    // ================================================================ output
    private static JsonArray BuildPlan(IEnumerable<PageItem> pages)
    {
        var plan = new JsonArray();
        foreach (var p in pages) plan.Add(new JsonObject { ["src"] = p.SrcPage, ["rotate"] = p.Rotation });
        return plan;
    }

    private async void OnPagesExport(object sender, RoutedEventArgs e)
    {
        if (_busy || _path == null || _pages.Count == 0) return;
        var save = new SaveFileDialog
        {
            Title = "輸出整理後的 PDF",
            Filter = "PDF 檔案 (*.pdf)|*.pdf",
            FileName = Path.GetFileNameWithoutExtension(_path) + "_整理.pdf",
            InitialDirectory = Path.GetDirectoryName(_path),
            AddExtension = true,
            DefaultExt = ".pdf",
        };
        if (save.ShowDialog(this) != true) return;
        var outPath = save.FileName;
        var plan = BuildPlan(_pages);
        var kept = _pages.Select(p => p.SrcPage).ToList();
        int rotated = _pages.Count(p => p.Rotation != 0);
        bool reordered = !kept.SequenceEqual(kept.OrderBy(x => x));

        JsonObject? result = null;
        await Run("整理頁面中…", async ct =>
        {
            result = (await _engine.CallAsync("arrange", new JsonObject { ["plan"] = plan, ["out"] = outPath, ["audit"] = true }, ct))!.AsObject();
        }, showBeam: false);
        UpdatePagesView();
        if (result == null) return;

        var deleted = result["deleted"]?.AsArray().Select(n => n!.GetValue<int>()).ToList() ?? new List<int>();
        var lines = new List<string> { $"已輸出 {result["pageCount"]?.GetValue<int>() ?? kept.Count} 頁。" };
        if (deleted.Count > 0) lines.Add($"已刪除第 {PageRange.Describe(deleted)} 頁。");
        if (rotated > 0) lines.Add($"旋轉了 {rotated} 頁。");
        if (reordered) lines.Add("頁面順序已調整。");
        SetStatus($"已輸出：{outPath}");
        new DoneDialog(outPath, string.Join("\n", lines), "整理完成",
            result["audit"]?["json"]?.GetValue<string>(), result["auditError"]?.GetValue<string>()) { Owner = this }.ShowDialog();
    }

    // ================================================================ split
    private void OnPagesSplitOptionChanged(object sender, RoutedEventArgs e)
    {
        if (PagesSplitEveryBox == null) return;   // fired while the XAML is still loading
        UpdatePagesSplitUi();
        if (!IsLoaded) return;
        if (PagesSplitEvery.IsChecked == true) PagesSplitEveryBox.Focus();
        else if (PagesSplitGroups.IsChecked == true) PagesSplitGroupsBox.Focus();
    }

    private void UpdatePagesSplitUi()
    {
        PagesSplitEveryBox.IsEnabled = PagesSplitEvery.IsChecked == true;
        PagesSplitGroupsBox.IsEnabled = PagesSplitGroups.IsChecked == true;
    }

    /// <summary>The groups of pages to write, each in the order they have in the list now.</summary>
    private string? BuildSplitGroups(out List<List<PageItem>> groups)
    {
        groups = new List<List<PageItem>>();
        if (PagesSplitEvery.IsChecked == true)
        {
            if (!int.TryParse(PagesSplitEveryBox.Text.Trim(), out int n) || n < 1) return "「每幾頁一份」請輸入 1 以上的整數。";
            for (int i = 0; i < _pages.Count; i += n) groups.Add(_pages.Skip(i).Take(n).ToList());
        }
        else if (PagesSplitGroups.IsChecked == true)
        {
            var parts = PagesSplitGroupsBox.Text.Split(new[] { ';', '；', '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)
                .Select(s => s.Trim()).Where(s => s.Length > 0).ToList();
            if (parts.Count == 0) return "請輸入頁碼分組，例如 1-3; 4-6; 7-（組與組之間用分號）。";
            for (int i = 0; i < parts.Count; i++)
            {
                if (!PageRange.TryParse(parts[i], _pageCount, out var wanted, out var error)) return $"第 {i + 1} 組「{parts[i]}」：{error}";
                var set = new HashSet<int>(wanted);
                var g = _pages.Where(p => set.Contains(p.SrcPage)).ToList();
                if (g.Count == 0) return $"第 {i + 1} 組「{parts[i]}」的頁面都已被刪除，沒有可輸出的頁。";
                groups.Add(g);
            }
        }
        else
        {
            foreach (var p in _pages) groups.Add(new List<PageItem> { p });
        }
        return groups.Count == 0 ? "沒有可輸出的頁面。" : null;
    }

    private static string UniquePdfPath(string folder, string stem)
    {
        var path = Path.Combine(folder, stem + ".pdf");
        for (int n = 2; File.Exists(path); n++) path = Path.Combine(folder, $"{stem}_{n}.pdf");   // never overwrite
        return path;
    }

    private async void OnPagesSplit(object sender, RoutedEventArgs e)
    {
        if (_busy || _path == null || _pages.Count == 0) return;
        var error = BuildSplitGroups(out var groups);
        if (error != null)
        {
            MessageBox.Show(this, error, "PdfTailor", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }

        var dlg = new OpenFolderDialog { Title = "選擇拆分後檔案要存放的資料夾", InitialDirectory = Path.GetDirectoryName(_path) };
        if (dlg.ShowDialog(this) != true) return;
        var folder = dlg.FolderName;
        var baseName = Path.GetFileNameWithoutExtension(_path);

        var created = new List<string>();
        int outPages = 0, auditFailed = 0;
        bool cancelled = false;
        await Run("拆分中…", async ct =>
        {
            try
            {
                for (int i = 0; i < groups.Count; i++)
                {
                    ct.ThrowIfCancellationRequested();
                    SetStatus($"拆分中 {i + 1} / {groups.Count}…（Esc 取消）");
                    var g = groups[i];
                    var name = $"{baseName}_p{PageRange.Describe(g.Select(p => p.SrcPage).OrderBy(x => x).ToList()).Replace(", ", "_")}";
                    var outPath = UniquePdfPath(folder, name);
                    var res = (await _engine.CallAsync("arrange",
                        new JsonObject { ["plan"] = BuildPlan(g), ["out"] = outPath, ["audit"] = true }, ct))!.AsObject();
                    created.Add(outPath);
                    outPages += g.Count;
                    if (res["auditError"] != null) auditFailed++;
                }
            }
            catch (OperationCanceledException) { cancelled = true; }
        }, showBeam: false);
        UpdatePagesView();
        if (created.Count == 0) return;

        bool complete = created.Count == groups.Count;
        var lines = new List<string> { complete
            ? $"已拆分成 {created.Count} 份 PDF，共 {outPages} 頁。"
            : $"{(cancelled ? "已取消" : "中途停止")}：已輸出 {created.Count} / {groups.Count} 份（共 {outPages} 頁），已輸出的檔案保留。" };
        lines.Add("原始檔未更動；每份旁邊都有修改紀錄（.audit.json / .audit.csv）。");
        if (auditFailed > 0) lines.Add($"其中 {auditFailed} 份的修改紀錄寫入失敗。");
        SetStatus(complete ? $"拆分完成：{created.Count} 份。" : $"拆分未完成：已輸出 {created.Count} / {groups.Count} 份。");
        new DoneDialog(folder, string.Join("\n", lines), complete ? "拆分完成" : "拆分未完成") { Owner = this }.ShowDialog();
    }
}
