namespace AndroidPhotoTransfer.Core.Media
{
    /// <summary>Filtering, search and sorting over already-collected metadata (never rescans the phone).</summary>
    internal static class MediaQuery
    {
        public static List<T> Apply<T>(IEnumerable<T> source, Func<T, MediaItem> item, Func<MediaItem, bool>? scope,
            MediaTypeFilter typeFilter, string? search, SortMode sort)
        {
            var terms = string.IsNullOrWhiteSpace(search)
                ? Array.Empty<string>()
                : search.Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

            var filtered = source.Where(x =>
            {
                var m = item(x);
                if (scope != null && !scope(m)) return false;
                if (typeFilter == MediaTypeFilter.Photos && m.Kind != MediaKind.Photo) return false;
                if (typeFilter == MediaTypeFilter.Videos && m.Kind != MediaKind.Video) return false;
                foreach (var term in terms)
                {
                    if (!m.Name.Contains(term, StringComparison.OrdinalIgnoreCase) &&
                        !m.Folder.Contains(term, StringComparison.OrdinalIgnoreCase) &&
                        !m.Extension.Contains(term, StringComparison.OrdinalIgnoreCase))
                    {
                        return false;
                    }
                }
                return true;
            });

            var list = filtered.ToList();
            Comparison<T> comparison = sort switch
            {
                SortMode.OldestFirst => (a, b) => Compare(item(a).Date, item(b).Date, item(a), item(b)),
                SortMode.NameAscending => (a, b) => NameCompare(item(a), item(b)),
                SortMode.NameDescending => (a, b) => NameCompare(item(b), item(a)),
                SortMode.LargestFirst => (a, b) => ThenName(item(b).Size.CompareTo(item(a).Size), item(a), item(b)),
                SortMode.SmallestFirst => (a, b) => ThenName(item(a).Size.CompareTo(item(b).Size), item(a), item(b)),
                _ => (a, b) => Compare(item(b).Date, item(a).Date, item(b), item(a))
            };
            list.Sort(comparison);
            return list;
        }

        private static int Compare(DateTime? a, DateTime? b, MediaItem x, MediaItem y) =>
            ThenName((a ?? DateTime.MinValue).CompareTo(b ?? DateTime.MinValue), x, y);

        private static int ThenName(int primary, MediaItem a, MediaItem b) => primary != 0 ? primary : NameCompare(a, b);

        private static int NameCompare(MediaItem a, MediaItem b)
        {
            int byName = StringComparer.OrdinalIgnoreCase.Compare(a.Name, b.Name);
            return byName != 0 ? byName : StringComparer.Ordinal.Compare(a.Path, b.Path);
        }
    }
}
