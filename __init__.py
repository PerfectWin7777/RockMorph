# RockMorph — geomorphology workstation for QGIS
# Copyright (C) 2025-2026 Tony Winter <wintertony7777@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; either version 2 of the License, or (at your option)
# any later version. See the LICENSE file.
# SPDX-License-Identifier: GPL-2.0-or-later

def classFactory(iface):
    from .plugin import RockMorphPlugin
    return RockMorphPlugin(iface)