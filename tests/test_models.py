from backend.models import DisplayState, TrackInfo


class TestTrackInfo:
    def test_display_key_lowercase(self):
        track = TrackInfo(title="Bohemian Rhapsody", artist="Queen")
        assert track.display_key == "queen:bohemian rhapsody"

    def test_display_key_dedup_match(self):
        a = TrackInfo(title="Hey Jude", artist="The Beatles")
        b = TrackInfo(title="Hey Jude", artist="The Beatles", album="Past Masters")
        assert a.display_key == b.display_key

    def test_display_key_different_tracks(self):
        a = TrackInfo(title="Hey Jude", artist="The Beatles")
        b = TrackInfo(title="Let It Be", artist="The Beatles")
        assert a.display_key != b.display_key

    def test_optional_fields_default_none(self):
        track = TrackInfo(title="Test", artist="Test Artist")
        assert track.album is None
        assert track.cover_url is None
        assert track.cover_url_hires is None
        assert track.year is None
        assert track.genre is None
        assert track.label is None

    def test_all_fields(self):
        track = TrackInfo(
            title="Blue Train",
            artist="John Coltrane",
            album="Blue Train",
            cover_url="http://example.com/cover.jpg",
            cover_url_hires="http://example.com/cover_hires.jpg",
            year="1957",
            genre="Jazz",
            label="Blue Note",
        )
        assert track.title == "Blue Train"
        assert track.label == "Blue Note"


class TestDisplayState:
    def test_enum_values(self):
        assert DisplayState.LISTENING == "listening"
        assert DisplayState.IDENTIFIED == "identified"
        assert DisplayState.IDLE == "idle"

    def test_string_comparison(self):
        assert DisplayState.IDLE == "idle"
        assert DisplayState.IDLE != "listening"
