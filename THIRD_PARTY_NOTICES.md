# Third-party notices

The MIT license in `LICENSE` covers this project's own code. The following are
included in, or used to build, this repository under their own terms.

## Spleen (font)

`tools/spleen-5x8.bdf` is the Spleen 5x8 bitmap font, and the generated
headers `dsclient/source/font.h`, `font4x6.h` and `font5x7.h` are derived from
it by `tools/genfont.py`; `dsterm/dsfont.py`, which the server package ships for
`dsterm view`, is copied from those headers by `tools/genviewfont.py`.

```
Copyright (c) 2018-2026, Frederic Cambus
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

  * Redistributions of source code must retain the above copyright
    notice, this list of conditions and the following disclaimer.

  * Redistributions in binary form must reproduce the above copyright
    notice, this list of conditions and the following disclaimer in the
    documentation and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

## BlocksDS build template

`dsclient/Makefile` and `dsclient/Makefile.arm7` are based on the BlocksDS
example Makefiles by Antonio Niño Díaz, released under CC0-1.0 (public domain
dedication); their SPDX headers are kept. The ROM links against BlocksDS's
libnds, dswifi, Maxmod and libc, which are distributed under their own
(permissive) licenses as part of the toolchain and are not vendored here.

## BlocksDS default ARM7 core

`dsclient/arm7/source/main.c` is modified from the default ARM7 core of
BlocksDS (`sys/arm7/main_core`), Zlib license: Copyright (C) 2005 Michael
Noland (joat), Copyright (C) 2005 Jason Rogers (Dovoto), Copyright (C)
2005-2015 Dave Murphy (WinterMute), Copyright (C) 2023 Antonio Niño Díaz. The
modification (Wi-Fi updates guarded against nesting) is marked in the file.

```
This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.

2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.

3. This notice may not be removed or altered from any source
   distribution.
```

## Python dependencies (installed by pip, not vendored)

- [pyte](https://github.com/selectel/pyte): LGPL-3.0
- [wcwidth](https://github.com/jquast/wcwidth): MIT

## Sounds

The sound set in `dsclient/sounds/` was generated with
[ElevenLabs](https://elevenlabs.io) sound-effect generation for this project,
then cut, re-pitched and arranged for the DS (see `tools/sfx_build.py`). It is
distributed with the project under its MIT license.
