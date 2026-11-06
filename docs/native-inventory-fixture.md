The disposition fixture replaces HOME, hiding the active system interpreter's user-site pytest; its nested full log contains `No module named pytest`, so no native inventory is emitted.
Main 5fc42a39 landed through the project test virtualenv (receipt 871c2fa7ec8a0996f9b32dd925768889), which supplies pytest independently of HOME; the later solo matrix used system Python.
Preserve the active interpreter's user-site base explicitly in this nested-pytest fixture when user sites are enabled; keep HOME isolated and all inventory, failure and quarantine assertions intact.
