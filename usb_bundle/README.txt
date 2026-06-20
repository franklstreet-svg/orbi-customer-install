ORBI INSTALLATION USB DRIVE
===========================

This drive contains everything needed to install Orbi at a customer's office
OR set up Frank's brain machine. No internet required at install time.

HOW TO USE
----------

1. At the install location, plug this drive into the computer.
2. Open a terminal (Ctrl+Alt+T on Ubuntu).
3. Type:    cd /media/$USER/<drive-label>  (or wherever the drive mounted)
4. Type:    sudo bash start.sh
5. The installer will ask: "Brain machine, or customer install?" — pick one
   and follow the prompts.

WHAT'S ON HERE
--------------

  source/orbi_web/        The complete Orbi codebase (~700KB)
  models/                 Pre-downloaded LLM model files
                          - llama-3.2-3b: offline tier for customer installs
                          - llama-3.1-8b: main model for the brain machine
                            (downloaded on first use if not present)
  start.sh                Linux bootstrap (run this with sudo)
  start.bat               Windows bootstrap (informational only — Orbi needs Linux)
  README.txt              This file

TIME ESTIMATES
--------------

  Brain machine install:   ~10-30 min (uses bundled 8B model if present,
                                       otherwise downloads it ~6.5GB)
  Customer install:        ~10-15 min total per customer

SUPPORT
-------

If anything fails, the installer prints exact next steps. The two installers
are also runnable manually:

  source/orbi_web/brain/install_brain.sh           (brain machine)
  source/orbi_web/customer_install/install.sh      (customer install)

Frank Street — Orbi
