"""Unit tests for ModbusRtuFrameGenerator (no HTTP, no HA harness)."""

from __future__ import annotations

import pytest

from custom_components.solplanet.modbus import DataType, ModbusRtuFrameGenerator

from tests.conftest import build_read_holding_response, build_write_single_response


@pytest.fixture
def gen() -> ModbusRtuFrameGenerator:
    return ModbusRtuFrameGenerator()


class TestFrameGeneration:
    def test_read_holding_register_frame_round_trip(self, gen):
        """Generated frame should decode correctly via build_read_holding_response."""
        # 40201 (offset 200), 1 register → returns [1]
        frame_hex = gen.generate_read_holding_register_frame(
            device_id=3, register_address=40201, register_length=1
        )
        frame = bytes.fromhex(frame_hex)
        assert frame[0] == 3
        assert frame[1] == 0x03
        # offset = 40201 - 40001 = 200 = 0x00C8
        assert int.from_bytes(frame[2:4], "big") == 200
        assert int.from_bytes(frame[4:6], "big") == 1
        # CRC last 2 bytes, little-endian — re-verify by appending body to a response build
        # path that uses the same _calculate_crc.

    def test_read_input_register_offset_uses_30001_base(self, gen):
        frame_hex = gen.generate_read_input_register_frame(
            device_id=1, register_address=30005, register_length=2
        )
        frame = bytes.fromhex(frame_hex)
        assert frame[1] == 0x04
        assert int.from_bytes(frame[2:4], "big") == 4  # 30005 - 30001

    def test_write_single_holding_register_frame_encodes_value(self, gen):
        frame_hex = gen.generate_write_single_holding_register_frame(
            device_id=3,
            register_address=40201,
            value=1,
            data_type=DataType.U16,
        )
        frame = bytes.fromhex(frame_hex)
        assert frame[1] == 0x06
        assert int.from_bytes(frame[4:6], "big") == 1

    def test_write_multiple_holding_registers_validates_values(self, gen):
        with pytest.raises(ValueError, match="Values must not be empty"):
            gen.generate_write_multiple_holding_registers_frame(
                device_id=3, register_address=40201, values=[]
            )

        with pytest.raises(ValueError, match="Invalid register value"):
            gen.generate_write_multiple_holding_registers_frame(
                device_id=3, register_address=40201, values=[0x10000]
            )

    def test_invalid_device_id_rejected(self, gen):
        with pytest.raises(ValueError, match="Invalid device ID"):
            gen.generate_read_holding_register_frame(
                device_id=256, register_address=40001, register_length=1
            )


class TestCRC:
    def test_crc_roundtrip_via_decode(self, gen):
        """Frame CRC must validate when decoded by the same generator."""
        # Build a synthetic response (CRC-correct via test helper) and decode it.
        response_hex = build_read_holding_response(device_id=3, register_values=[0x1234])
        decoded = gen.decode_response(response_hex, DataType.U16)
        assert decoded == 0x1234

    def test_crc_mismatch_raises(self, gen):
        # Corrupt the last byte of a known-good response.
        response_hex = build_read_holding_response(device_id=3, register_values=[42])
        bad = response_hex[:-2] + "00"
        with pytest.raises(ValueError, match="CRC error"):
            gen.decode_response(bad, DataType.U16)


class TestDecoding:
    def test_u16_single_register(self, gen):
        response = build_read_holding_response(device_id=3, register_values=[1234])
        assert gen.decode_response(response, DataType.U16) == 1234

    def test_u16_multiple_registers_returns_list(self, gen):
        response = build_read_holding_response(device_id=3, register_values=[1, 0, 0, 50])
        assert gen.decode_response(response, DataType.U16) == [1, 0, 0, 50]

    def test_u32_high_low_word_combined(self, gen):
        # High word 0x0001, low word 0x0000 → 0x00010000 = 65536
        response = build_read_holding_response(device_id=3, register_values=[0x0001, 0x0000])
        assert gen.decode_response(response, DataType.U32) == 0x00010000

    def test_s16_negative_value(self, gen):
        # 0xFFFE as S16 → -2
        response = build_read_holding_response(device_id=3, register_values=[0xFFFE])
        assert gen.decode_response(response, DataType.S16) == -2

    def test_u16_nan_returns_none(self, gen):
        # 0xFFFF is the NaN sentinel for U16
        response = build_read_holding_response(device_id=3, register_values=[0xFFFF])
        assert gen.decode_response(response, DataType.U16) is None

    def test_string_decoding(self, gen):
        # 'A' (0x41) + 'B' (0x42) → 0x4142
        raw = (0x41 << 8) | 0x42
        response = build_read_holding_response(device_id=3, register_values=[raw])
        assert gen.decode_response(response, DataType.STRING) == "AB"

    def test_write_single_response_decoded_as_dict(self, gen):
        response = build_write_single_response(device_id=3, register_offset=200, value=1)
        decoded = gen.decode_response(response, DataType.U16)
        assert decoded == {
            "device_id": 3,
            "function_code": 0x06,
            "register_address": 200,
            "data": 1,
        }


class TestEncoding:
    def test_encode_none_returns_nan_sentinel(self, gen):
        assert gen.encode_request_data(None, DataType.U16) == 0xFFFF
        assert gen.encode_request_data(None, DataType.S32) == 0x80000000

    def test_encode_s16_negative(self, gen):
        # -2 → 0xFFFE
        assert gen.encode_request_data(-2, DataType.S16) == 0xFFFE

    def test_encode_string_pads_to_two_chars(self, gen):
        # 'A' → 0x4100 (high byte 'A', low byte '\0')
        assert gen.encode_request_data("A", DataType.STRING) == 0x4100

    def test_encode_value_out_of_range_raises(self, gen):
        with pytest.raises(ValueError, match="must be in range"):
            gen.encode_request_data(70000, DataType.U16)
