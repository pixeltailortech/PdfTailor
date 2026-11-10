using System.ComponentModel;
using System.Windows.Media.Imaging;

namespace PdfTailor.Services;

/// <summary>One page in the "整理頁面" list: which original page it is, how much it is
/// rotated (clockwise, on top of what the PDF already has) and where it sits now.</summary>
public sealed class PageItem : INotifyPropertyChanged
{
    private int _rotation;
    private int _order;

    public PageItem(int srcPage, BitmapImage thumb, double thumbWidth, double thumbHeight)
    {
        SrcPage = srcPage;
        Thumb = thumb;
        ThumbWidth = thumbWidth;
        ThumbHeight = thumbHeight;
    }

    /// <summary>1-based page number in the original PDF.</summary>
    public int SrcPage { get; }
    public BitmapImage Thumb { get; }
    public double ThumbWidth { get; }
    public double ThumbHeight { get; }
    public string Label => $"第 {SrcPage} 頁";

    /// <summary>Extra clockwise rotation: 0, 90, 180 or 270.</summary>
    public int Rotation
    {
        get => _rotation;
        set
        {
            value = ((value % 360) + 360) % 360;
            if (_rotation == value) return;
            _rotation = value;
            Raise(nameof(Rotation));
            Raise(nameof(RotationLabel));
        }
    }

    public string RotationLabel => _rotation == 0 ? "" : $"↻ {_rotation}°";

    /// <summary>1-based position in the output.</summary>
    public int Order
    {
        get => _order;
        set
        {
            if (_order == value) return;
            _order = value;
            Raise(nameof(Order));
        }
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    private void Raise(string name) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
