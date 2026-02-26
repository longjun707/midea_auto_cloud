# -*- coding: utf-8 -*-
from Crypto.Cipher import AES
import binascii

SSE_DECRYPT_KEY = b'a1a971846865c9a6'
encrypted_hex = 'F65AEB505950A751F3B6C9F630F92123923F689FFCC31FE120516F7D458BD6ACC69E09FC3A05B193857F9ABCC1A137B54236AB1F9BBF078E764FE199A9F3AE4FF014B07EF2A0A30FEB122B7B337184385F684597364B8C66EBCC586093E731AD5D1FECD0DC2A47A64B1392D5ECA3BC6232E2580A80E486BF3EB02CF2BC3C6CCBD145170A7E52AED6F7548C4A1B8E8DDEEC2892998C81C8E414481EB72E915D335F3248FD14F9D6986E52FC8FB7F9FEE2EF3641B42AFD82ACE0D44C99983805E52F8923650789BD8152CE963EB86925558A5A8BB4E40DF4D0E0DCC38342969A892FB49F129BB07B3D12CB729C638A97A6CCF0E13C5ECBFDEF1EFEF789083926968E7BB2BCFCC5CCD4C1E35D6145D32E0578CBAEC00BF2DB119A8B416B59192D1D838B80D2E37DA9874476373A1988787DA93E61DD5B2B6B0C934982CFD103F6A8840022397B43AB717767FF8E276CD1E294CA7D7B8C89B917A87340047A3E24778B206787B2A236BE2741F553B7153911FAA401F535D7880A6B72DA87D3410FF4837DF865046CB3318D67FBB2ACCAF74ED11FDCAA222E79DBAD59C7F5526F85D993D096FD754ACC82983F56DB43F203D56BEFEE34EBED32CA19C5A55DA447753F349A1901ABC7032BBA5D48DECBB8CACAD5526E44C6A19022840EC4B9B018381DCA263FD58E7A7DD242E4AAE71253C2446D73BFE53ECBD030C2A495D3D73BB9B1827D364A78E88EAF201E181B9A3CE3CE0D6E9E4652EF7B9FB390B73718BBEB3D2CBAA38D53DE466D93744C532CD02E2894606C3AC7B89C7AE2DCC235A90FEA2928FDFBF4F7566BF2F43E0DCEAF28F7632A115BC4A624C8744E546CD8DD7D24FF4A8765836BB70106D4444A2467B367E4DF8577B9C7AAA3808549C2D16D0A16A0C15D2C644AFD5ED866962F456D56B4C3047244480F78A5774721D93A780972655B3B964FB985F8899B61F6C911B68D428E1D54D2257C25319F69CCD1162A230926338D5CE3A732BB57547BA75A467A3C416833C8F4DF262F877D403B8F515273EE62B2330A03F81E19002609EC53579A509287652DD2BDC3FA5E2E3C20C9FF97EB79A963D6933F3214C29F2ED2D303BC0997FC82B65244F72503EF1B649A3959A6B8B0AC4E9E127BE3AEB81ADD0ADB0AA4B9273C0889A03746AEE11E0C6F8E52CBD3A6AF67BAEEEFF7D200ADF7EADDBFBE2112EC5014B849CA601C94C936C3DC4CDDAF4EC169615D5CED36EF34E096A65E90A81219292D7F75D5F36A76B466C126C1C066D4165DF7B750DED149F7F8822F097F2A438F66E5A94FC98E0301AA8B96754FEC7FA7C1539C543B2A62B95BDA55A0E14FA62ECBB99AEEF5A8557E543C0B29473B4C3356871C75D64AD145F3355F684597364B8C66EBCC586093E731AD5F684597364B8C66EBCC586093E731AD2FBE9222B55B469D447559B172B0CBB6'

enc_data = binascii.unhexlify(encrypted_hex)
cipher = AES.new(SSE_DECRYPT_KEY, AES.MODE_ECB)
decrypted = cipher.decrypt(enc_data)
pad = decrypted[-1]
if 1 <= pad <= 16:
    decrypted = decrypted[:-pad]
text = decrypted.decode('utf-8')
bytes_list = [int(x.strip()) for x in text.split(',') if x.strip()]
print(f'Total bytes: {len(bytes_list)}')
print(f'bytes[0:50] = {bytes_list[:50]}')
print(f'bytes[41] (power) = {bytes_list[41]}')
print()

# Parse TLV
props = {}
i = 43
while i < len(bytes_list) - 2:
    if bytes_list[i] == 255:
        prop_id = bytes_list[i+1]
        length = bytes_list[i+2]
        if i + 3 + length <= len(bytes_list):
            value = bytes_list[i+3:i+3+length]
            props[prop_id] = value[0] if len(value)==1 else value
            i += 3 + length
            continue
    i += 1

print('=== TLV Properties ===')
for pid, val in sorted(props.items()):
    print(f'  prop {pid:3d}: {val}')

print()
print('=== Decoded Status ===')

# Power
power = bytes_list[41]
print(f'Power: {"ON" if power==1 else "OFF"}')

# Mode (prop 3)
mode_map = {1:'cool', 2:'fan', 3:'heat', 4:'auto', 5:'dry'}
if 3 in props:
    print(f'Mode: prop3={props[3]} -> {mode_map.get(props[3],"unknown")}')

# Fan speed (prop 2)
if 2 in props and isinstance(props[2], list) and len(props[2])>=4:
    fs = props[2][3]
    fan_str = "auto" if fs==102 else f"{fs}档"
    print(f'Fan speed: prop2={props[2]} -> speed={fs} ({fan_str})')

# Indoor temp (prop 66)
if 66 in props:
    if isinstance(props[66], list) and len(props[66])>=2:
        print(f'Indoor temp: prop66={props[66]} -> {props[66][1]/10} C')
    else:
        print(f'Indoor temp: prop66={props[66]}')

# Outdoor temp (prop 10)
if 10 in props:
    print(f'Outdoor temp: prop10={props[10]} -> {props[10]/10} C')

# Target temp - try multiple props
if 13 in props:
    if isinstance(props[13], list) and len(props[13])>=2:
        print(f'Target temp (prop13): {props[13]} -> {props[13][1]/10} C')
    elif isinstance(props[13], list) and len(props[13])>=3:
        print(f'Target temp (prop13): {props[13]}')
    else:
        print(f'Target temp (prop13): {props[13]}')

if 6 in props:
    if isinstance(props[6], int):
        print(f'Target temp (prop6): raw={props[6]} -> (raw-40)/2 = {(props[6]-40)/2} C')
    else:
        print(f'Prop 6: {props[6]}')

if 4 in props:
    print(f'Prop 4: {props[4]}')
if 5 in props:
    print(f'Prop 5: {props[5]}')
if 72 in props:
    print(f'Prop 72: {props[72]}')
if 73 in props:
    print(f'Prop 73: {props[73]}')

# Also check bytes[8] from header
print()
print('=== Header Analysis ===')
print(f'bytes[3] = {bytes_list[3]}')
print(f'bytes[8] = {bytes_list[8]}')
print(f'bytes[20] = {bytes_list[20]}')
print(f'bytes[20] as Lua indoor = {(bytes_list[20]-40)/2} C')
