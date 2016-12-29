from subprocess import call
import argparse
import array
import struct
import binascii

MAX_FRAME_LEN = 255
MAX_FRAME_COUNT = 255

bitswap = b''.join(chr(sum(((val >> i) & 1) << (7 - i) for i in range(8))) for val in range(256))
to_uint_be = lambda data: struct.pack('>I', data)

def create_ogg_packet(bos, eos, granule, serial_no, packet_no, segments):
    header_type = (1 << 1) if bos else 0    # b_o_s
    header_type |= (1 << 2) if eos else 0   # e_o_s

    ogg = 'OggS'                            # 4
    ogg += struct.pack('B', 0)              # 1, version - must be 0
    ogg += struct.pack('B', header_type)    # 1, Header type
    ogg += struct.pack('q', granule)        # 8, granule position
    ogg += struct.pack('I', serial_no)      # 4, bitstream serial no
    ogg += struct.pack('I', packet_no)      # 4, packet number
    ogg += struct.pack('i', 0)              # 4, crc - evaluated later
    ogg += struct.pack('B', len(segments))  # 1, Number of segments
    for s in segments:                      # add length of each segment
        ogg += struct.pack('B', len(s))     # 1, length of segment
    ogg += ''.join(segments)

    # crc = binascii.crc32(ogg) & 0xffffffff  # calulate crc over whole packet

    import zlib
    crc = (~zlib.crc32(ogg.translate(bitswap), -1)) & 0xffffffff
    crc = to_uint_be(crc).translate(bitswap)

    return ogg[:22] + crc + ogg[26:]

def create_speex_header(version, rate, frame_sz):
    bitstream_version  = 4
    mode = 1 if rate == 16000 else 0
    bitrate = 12800 if rate == 16000 else 8000

    version = version[:20]
    version = version.ljust(20, '\0')

    spx = "Speex   "
    spx += version
    spx += struct.pack('i', 1)    # version - must be 1
    spx += struct.pack('i', 80)   # header size
    spx += struct.pack('I', rate) # sample rate
    spx += struct.pack('i', mode) # mode
    spx += struct.pack('i', bitstream_version)    # mode bitstream version
    spx += struct.pack('i', 1)    # number of channels
    spx += struct.pack('i', bitrate)    # bit-rate
    spx += struct.pack('I', frame_sz)   # frame size (number of PCM16 samples)
    spx += struct.pack('i', 0)    # variable bit rate (off)
    spx += struct.pack('i', 1)    # frames per packet 
    spx += struct.pack('i', 0)    # extra headers
    spx += struct.pack('i', 0)    # reserved
    spx += struct.pack('i', 0)    # reserved

    return spx

def create_vorbis_comment(vendor, user_comments):
    comment =  struct.pack('I', len(vendor))        # 4, vendor length
    comment += vendor                               # n, vendor string
    comment += struct.pack('I', len(user_comments)) # n, user comment list length
    for u in user_comments:
        comment += struct.pack('I', len(u))
        comment += u

    return comment


def store_data(frames, filename, rate):
    frame_sz = (rate / 1000) * 20
    version = "1.2rc1"
    serial_no = 0x42E296FC

    spx = create_speex_header(version, rate, frame_sz)
    ogg = create_ogg_packet(True, False, 0, serial_no, 0, [spx])
    comment = create_vorbis_comment('Encoded with Speex ' + version, [])
    ogg += create_ogg_packet(False, False, 0, serial_no, 1, [comment])

    packet_no = 2
    tot_granules = 0
    while (len(frames) > 0):
        packet_frames = min(len(frames), MAX_FRAME_COUNT)
        last_packet = (len(frames) == packet_frames)

        tot_granules += packet_frames * frame_sz
        granule_pos = tot_granules - frame_sz
        
        ogg += create_ogg_packet(False, last_packet, granule_pos, serial_no, packet_no, frames[:packet_frames])
        
        frames = frames[packet_frames:]
        packet_no += 1

    with open(filename, 'wb') as f:
        f.write(ogg)
        f.close()

    return filename

