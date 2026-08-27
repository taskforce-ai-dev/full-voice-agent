"""Regression coverage for the direct SmartPBX English welcome audio seam."""

import asyncio
import audioop
import unittest


def _mulaw(samples):
    pcm = b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples)
    return audioop.lin2ulaw(pcm, 2)


class SmartPBXWelcomeAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import server

        server._SMARTPBX_WELCOME_AUDIO_CACHE.clear()

    def test_fade_preserves_leading_silence_and_post_fade_bytes(self):
        import server

        leading = b"\xff" * 17
        onset = _mulaw([12_000] * 600)
        raw = leading + onset + b"\xff" * 9

        processed = server._fade_smartpbx_welcome_mulaw(raw)
        decoded_raw = audioop.ulaw2lin(raw, 2)
        decoded_processed = audioop.ulaw2lin(processed, 2)

        self.assertEqual(processed[:len(leading)], leading)
        self.assertEqual(processed[len(leading) + 480:], raw[len(leading) + 480:])
        self.assertEqual(decoded_processed[len(leading) * 2:len(leading) * 2 + 2], b"\x00\x00")
        self.assertGreater(
            abs(int.from_bytes(decoded_processed[(len(leading) + 240) * 2:(len(leading) + 241) * 2], "little", signed=True)),
            abs(int.from_bytes(decoded_processed[(len(leading) + 10) * 2:(len(leading) + 11) * 2], "little", signed=True)),
        )
        self.assertEqual(
            decoded_processed[(len(leading) + 479) * 2:(len(leading) + 480) * 2],
            decoded_raw[(len(leading) + 479) * 2:(len(leading) + 480) * 2],
        )

    def test_fade_degrades_to_raw_bytes_when_audioop_is_unavailable(self):
        import server

        raw = _mulaw([0, 12_000, 12_000])
        original = server.audioop
        try:
            server.audioop = None
            self.assertEqual(server._fade_smartpbx_welcome_mulaw(raw), raw)
        finally:
            server.audioop = original

    def test_cache_key_changes_with_all_canonical_request_bytes_inputs(self):
        import server

        common = ("Welcome", "https://example.test/voice?output_format=ulaw_8000", "flash", {"stability": 0.5})
        key = server._smartpbx_welcome_audio_cache_key(*common)
        self.assertNotEqual(key, server._smartpbx_welcome_audio_cache_key("Welcome!", *common[1:]))
        self.assertNotEqual(key, server._smartpbx_welcome_audio_cache_key(common[0], common[1] + "&optimize_streaming_latency=3", *common[2:]))
        self.assertNotEqual(key, server._smartpbx_welcome_audio_cache_key(common[0], common[1], "other-model", common[3]))
        self.assertNotEqual(key, server._smartpbx_welcome_audio_cache_key(common[0], common[1], common[2], {"stability": 0.4}))

    async def test_cache_hit_avoids_a_second_provider_fetch(self):
        import server

        fetches = 0

        async def fetch():
            nonlocal fetches
            fetches += 1
            return _mulaw([12_000] * 600)

        key = server._smartpbx_welcome_audio_cache_key("Welcome", "url", "model", {"stability": 0.5})
        first = await server._get_cached_smartpbx_welcome_audio(key, fetch)
        second = await server._get_cached_smartpbx_welcome_audio(key, fetch)

        self.assertEqual(fetches, 1)
        self.assertEqual(first, second)

    async def test_concurrent_cold_greetings_publish_one_immutable_cache_value(self):
        import server

        entered = asyncio.Event()
        release = asyncio.Event()
        fetches = 0

        async def fetch():
            nonlocal fetches
            fetches += 1
            entered.set()
            await release.wait()
            return _mulaw([12_000] * 600)

        key = server._smartpbx_welcome_audio_cache_key("Welcome", "url", "model", {"stability": 0.5})
        first = asyncio.create_task(server._get_cached_smartpbx_welcome_audio(key, fetch))
        await entered.wait()
        second = asyncio.create_task(server._get_cached_smartpbx_welcome_audio(key, fetch))
        await asyncio.sleep(0)
        release.set()
        one, two = await asyncio.gather(first, second)

        self.assertEqual(fetches, 2)
        self.assertEqual(one, two)
        self.assertIsInstance(one, bytes)
        self.assertEqual(len(server._SMARTPBX_WELCOME_AUDIO_CACHE), 1)


if __name__ == "__main__":
    unittest.main()
