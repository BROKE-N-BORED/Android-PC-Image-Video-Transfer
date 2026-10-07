using System.Collections.ObjectModel;
using System.Collections.Specialized;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Windows;
using System.Windows.Data;
using AndroidPhotoTransfer.Infrastructure;
using Microsoft.Win32;

namespace AndroidPhotoTransfer.UI
{
    /// <summary>ObservableCollection that can swap its whole contents with a single Reset notification.</summary>
    public sealed class BulkObservableCollection<T> : ObservableCollection<T>
    {
        public void ReplaceAll(IEnumerable<T> items)
        {
            CheckReentrancy();
            Items.Clear();
            foreach (var item in items) Items.Add(item);
            OnPropertyChanged(new PropertyChangedEventArgs(nameof(Count)));
            OnPropertyChanged(new PropertyChangedEventArgs("Item[]"));
            OnCollectionChanged(new NotifyCollectionChangedEventArgs(NotifyCollectionChangedAction.Reset));
        }
    }

    /// <summary>A value with a display label, for ComboBoxes.</summary>
    public sealed record Option<T>(T Value, string Label);

    public sealed class InverseBooleanConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture) => value is not true;
        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) => value is not true;
    }

    public sealed class InverseBoolToVisibilityConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture) =>
            value is true ? Visibility.Collapsed : Visibility.Visible;

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) =>
            throw new NotSupportedException();
    }

    /// <summary>Visible when the bound number is greater than zero.</summary>
    public sealed class PositiveToVisibilityConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture) =>
            value is int n && n > 0 ? Visibility.Visible : Visibility.Collapsed;

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture) =>
            throw new NotSupportedException();
    }

    internal static class Dialogs
    {
        public static string? PickFolder(string? initialFolder, string title)
        {
            var dialog = new OpenFolderDialog { Title = title, Multiselect = false };
            if (!string.IsNullOrWhiteSpace(initialFolder) && Directory.Exists(initialFolder)) dialog.InitialDirectory = initialFolder;
            var owner = Application.Current?.Windows.OfType<Window>().FirstOrDefault(w => w.IsActive) ?? Application.Current?.MainWindow;
            return dialog.ShowDialog(owner) == true ? dialog.FolderName : null;
        }

        public static void OpenInExplorer(string folder)
        {
            try
            {
                Directory.CreateDirectory(folder);
                Process.Start(new ProcessStartInfo { FileName = folder, UseShellExecute = true });
            }
            catch (Exception ex)
            {
                Logger.Warn("Could not open folder in Explorer", ex);
            }
        }
    }
}
