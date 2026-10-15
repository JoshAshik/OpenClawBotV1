"""
Synthetic Apple Contacts store (AddressBook-v22.abcddb) for tests.

Only the three Core-Data tables contacts_module reads, with the real column names.
Phone numbers are deliberately stored in the loose formats Contacts keeps them in;
the handles below match tests/fixtures/make_chat_db.py so window titles resolve.
"""

import sqlite3
from pathlib import Path

MIKE_NAME = "Mike Plumber"
MIKE_PHONE_RAW = "(415) 555-0101"          # → +14155550101 == make_chat_db.MIKE
MIKE_PHONE_E164 = "+14155550101"
MIKE_EMAIL = "mike@plumbing.example"

SARA_NAME = "Sara Lee"
SARA_PHONE_RAW = "+1 415-555-0102"         # → +14155550102 == make_chat_db.SARA
SARA_PHONE_E164 = "+14155550102"
SARA_WORK_RAW = "415.555.0199 x12"         # extension stripped → +14155550199
SARA_WORK_E164 = "+14155550199"
SARA_EMAIL = "Sara.Lee@Example.com"        # stored mixed-case; resolver lower-cases

ORG_NAME = "Acme Property Services"
ORG_PHONE_RAW = "800 555 0100"
ORG_PHONE_E164 = "+18005550100"
ORG_EMAIL = "office@acme.example"

# A record with a name but no phone/email: must be dropped by load_contacts.
EMPTY_NAME = "Nobody Reachable"

_SCHEMA = """
CREATE TABLE ZABCDRECORD (
    Z_PK INTEGER PRIMARY KEY,
    Z_ENT INTEGER,
    Z_OPT INTEGER,
    ZFIRSTNAME VARCHAR,
    ZLASTNAME VARCHAR,
    ZORGANIZATION VARCHAR,
    ZNICKNAME VARCHAR,
    ZUNIQUEID VARCHAR
);
CREATE TABLE ZABCDPHONENUMBER (
    Z_PK INTEGER PRIMARY KEY,
    Z_ENT INTEGER,
    Z_OPT INTEGER,
    ZOWNER INTEGER,
    ZFULLNUMBER VARCHAR,
    ZLABEL VARCHAR
);
CREATE TABLE ZABCDEMAILADDRESS (
    Z_PK INTEGER PRIMARY KEY,
    Z_ENT INTEGER,
    Z_OPT INTEGER,
    ZOWNER INTEGER,
    ZADDRESS VARCHAR,
    ZLABEL VARCHAR
);
"""

RECORDS = [  # Z_PK, first, last, org, nick
    (1, "Mike", "Plumber", None, None),
    (2, "Sara", "Lee", None, None),
    (3, None, None, ORG_NAME, None),
    (4, "Nobody", "Reachable", None, None),
]
PHONES = [  # Z_PK, owner, number, label
    (1, 1, MIKE_PHONE_RAW, "_$!<Mobile>!$_"),
    (2, 2, SARA_PHONE_RAW, "_$!<Mobile>!$_"),
    (3, 2, SARA_WORK_RAW, "_$!<Work>!$_"),
    (4, 3, ORG_PHONE_RAW, "_$!<Main>!$_"),
]
EMAILS = [  # Z_PK, owner, address, label
    (1, 1, MIKE_EMAIL, "_$!<Work>!$_"),
    (2, 2, SARA_EMAIL, "_$!<Home>!$_"),
    (3, 3, ORG_EMAIL, "_$!<Work>!$_"),
]


def build_contacts_db(path: Path | str) -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO ZABCDRECORD (Z_PK, Z_ENT, Z_OPT, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME) "
            "VALUES (?, 18, 1, ?, ?, ?, ?)", RECORDS)
        conn.executemany(
            "INSERT INTO ZABCDPHONENUMBER (Z_PK, Z_ENT, Z_OPT, ZOWNER, ZFULLNUMBER, ZLABEL) "
            "VALUES (?, 22, 1, ?, ?, ?)", PHONES)
        conn.executemany(
            "INSERT INTO ZABCDEMAILADDRESS (Z_PK, Z_ENT, Z_OPT, ZOWNER, ZADDRESS, ZLABEL) "
            "VALUES (?, 7, 1, ?, ?, ?)", EMAILS)
        conn.commit()
    finally:
        conn.close()
    return path


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("AddressBook-v22.abcddb")
    print(build_contacts_db(out))
