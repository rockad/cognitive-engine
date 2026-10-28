"""A real SMB server for the tests: impacket's SimpleSMBServer over one directory.

    python smb_server.py <share_dir> <port> <user> <password>

Exports the directory as the share `KA` on 127.0.0.1. Test-only. Run it with
`uv run --isolated --no-project --with impacket python smb_server.py ...`: impacket must
NOT be installed in the project environment, because it drops a `smbclient.py` script into
bin/ that shadows smbprotocol's `smbclient` package under `uv run pytest`.
"""
import sys

from impacket import ntlm, smbserver

share_dir, port, user, password = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
srv = smbserver.SimpleSMBServer(listenAddress="127.0.0.1", listenPort=port)
srv.addShare("KA", share_dir, "lookup test share")
srv.setSMB2Support(True)
srv.addCredential(user, 1001, "aad3b435b51404eeaad3b435b51404ee", ntlm.compute_nthash(password).hex())
srv.setLogFile("")
srv.start()
