import uuid

def str_comprisedOnlyOf(s, chars):
    return (len(s.strip(chars)) == 0)

def is_valid_uuid_str(s):
    return (str_comprisedOnlyOf(s, "0123456789abcdef-") and (len(s) == 36))

def is_hex(s):
    try:
        int(s, 16)
        return True
    except:
        return False

def convert_to_bytes(s):
    s_bytes = s

    if type(s) is int:
        s_bytes = s
    elif type(s) is uuid.UUID:
        s_bytes = s.bytes
    elif is_hex(s):
        s_bytes = s.decode('hex')
    elif type(s) is str and is_valid_uuid_str(s):
        s_bytes = s.replace("-", "").decode('hex')
    elif type(s) is str:
        s_bytes = s_bytes.encode('UTF-8')
    # else assume bytes

    return s_bytes
