def classFactory(iface):
    from .plugin import Back2thePastPlugin
    return Back2thePastPlugin(iface)
