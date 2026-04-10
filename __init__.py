def classFactory(iface):
    from .plugin import RockMorphPlugin
    return RockMorphPlugin(iface)