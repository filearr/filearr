package extract

import (
	"fmt"
	"os"

	"github.com/dhowden/tag"
)

// extractAudioTags reads standard audio tags with the pure-Go dhowden/tag reader
// (already vendored for cover-art thumbnails): ID3v1/ID3v2, MP4 atoms, FLAC/Ogg
// Vorbis comments, DSF.
//
// Key names are central's audio vocabulary (tasks/extract.py extract_audio):
// title/artist/album/genre/year. The technical fields central also stores —
// duration/bitrate/samplerate/channels — are NOT available from a tag reader
// (they require decoding the stream), so they come from the ffprobe pass when
// the host has ffprobe. An empty tag set is a normal outcome, not an error.
func extractAudioTags(path string, res *Result) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open audio: %w", err)
	}
	defer f.Close()

	m, err := tag.ReadFrom(f)
	if err != nil {
		// tag.ReadFrom rejects a file with no recognisable tag container. That is
		// a fact about the file, not a failure of the pass — but recording it
		// keeps "why is this mp3 bare?" answerable from the errors surface.
		return fmt.Errorf("read audio tags: %w", err)
	}
	res.set("title", m.Title())
	res.set("artist", m.Artist())
	res.set("album", m.Album())
	res.set("genre", m.Genre())
	// dhowden/tag already parses the year to an int, so central's coerce_year
	// defence (a "2007-10-09" date string leaking into a typed column) has no
	// equivalent hazard here. 0 means "no year tag".
	if y := m.Year(); y > 0 {
		res.Meta["year"] = y
	}
	return nil
}
