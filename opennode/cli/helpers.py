import types

from snack import Textbox, Entry, Button, Listbox
from snack import ButtonBar, TextboxReflowed, CheckboxTree, GridFormHelp, SnackScreen
from snack import ButtonChoiceWindow, ListboxChoiceWindow


def SelectCheckboxWindow(screen, title, text, items, buttons = ('Ok', 'Cancel'),
            width = 40, scroll = 0, height = -1, help = None):
    """Helper class for displaying a windows with a checkbox list. 
    On exit, list of selected items is returned"""
    if (height == -1): height = len(items)
    if len(items) > height: scroll = 1
    bb = ButtonBar(screen, buttons)
    t = TextboxReflowed(width, text)
    cb = CheckboxTree(height, scroll = scroll)
    count = 0
    for count, item in enumerate(items):
        if isinstance(item, types.TupleType):
            (text, key, selected) = item
        else:
            text = item
            key = count
            selected = 0

        cb.append(text, key, selected)

    g = GridFormHelp(screen, title, help, 1, 3)
    g.add(t, 0, 0)
    g.add(cb, 0, 1, padding = (0, 1, 0, 1))
    g.add(bb, 0, 2, growx = 1)
    rc = g.runOnce()
    return (bb.buttonPressed(rc), cb.getSelection())

def CreateTemplateWindow(screen, title, vm_type, templates, help = None):
    """Helper class for displaying a form for creating a new VM template"""
    label_base = Textbox(40, 2, 'Select %s VM to be used as a template' %vm_type, 0, 0)
    
    base_tmpl = Listbox(7, 1, 0, 30, 1)
    for vm in templates.keys():
        base_tmpl.append(templates[vm], vm)
    
    label_newname = Textbox(40, 2, 'Name of the template to be created', 0, 0)
    spacer1 = Textbox(1, 1, "", 0, 0)
    spacer2 = Textbox(1, 1, "", 0, 0)
    entry_newname = Entry(30, 'template_name')
    bb = ButtonBar(screen, ('Create new template', 'Main menu'))
    form = GridFormHelp(screen, title, help, 1, 7)
    form.add(label_base, 0, 0)
    form.add(base_tmpl, 0, 1)
    form.add(spacer1, 0, 2)
    form.add(label_newname, 0, 3)
    form.add(entry_newname, 0, 4)
    form.add(spacer2, 0, 5)
    form.add(bb, 0, 6)
    form_result = form.runOnce()
    return (bb.buttonPressed(form_result), templates[base_tmpl.current()], entry_newname.value())

def display_selection(screen, title, list_of_items, subtitle, default = None):
    """Display a list of items, return selected one or None, if nothing was selected"""
    if len(list_of_items) > 0:
        if not isinstance(list_of_items[0], types.TupleType):
            # if we have a list of strings, we'd prefer to get these strings as the selection result
            list_of_items = zip(list_of_items, list_of_items)
        height = 10
        scroll = 1 if len(list_of_items) > height else 0
        action, selection = ListboxChoiceWindow(screen, title, subtitle, list_of_items, 
                            ['Ok', 'Back'], scroll = scroll, height = height, default = default)
        if action != 'back':
            return selection
    else:
        ButtonChoiceWindow(screen, title, 'Sorry, there are no items to choose from', ['Back'])
    return None

def display_checkbox_selection(screen, title, list_of_items, subtitle):
    if len(list_of_items) > 0:            
        action, selection = SelectCheckboxWindow(screen, title, subtitle, list_of_items, ['Ok', 'Back'], height = 10)
        if action != 'back':
            return selection
    else:
        ButtonChoiceWindow(screen, title, 'Sorry, there are no items to choose from', ['Back'])
    return None
