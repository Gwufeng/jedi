"""
Functions inferring the syntax tree.
"""
import copy

from parso.python import tree

from jedi._compatibility import force_unicode, unicode
from jedi import debug
from jedi import parser_utils
from jedi.inference.base_value import ValueSet, NO_VALUES, ValueualizedNode, \
    ValueualizedName, iterator_to_value_set, iterate_values
from jedi.inference.lazy_value import LazyTreeValue
from jedi.inference import compiled
from jedi.inference import recursion
from jedi.inference import helpers
from jedi.inference import analysis
from jedi.inference import imports
from jedi.inference import arguments
from jedi.inference.value import ClassValue, FunctionValue
from jedi.inference.value import iterable
from jedi.inference.value import TreeInstance
from jedi.inference.finder import NameFinder
from jedi.inference.helpers import is_string, is_literal, is_number
from jedi.inference.compiled.access import COMPARISON_OPERATORS
from jedi.inference.cache import inference_state_method_cache
from jedi.inference.gradual.stub_value import VersionInfo
from jedi.inference.gradual import annotation
from jedi.inference.value.decorator import Decoratee
from jedi.plugins import plugin_manager


def _limit_value_infers(func):
    """
    This is for now the way how we limit type inference going wild. There are
    other ways to ensure recursion limits as well. This is mostly necessary
    because of instance (self) access that can be quite tricky to limit.

    I'm still not sure this is the way to go, but it looks okay for now and we
    can still go anther way in the future. Tests are there. ~ dave
    """
    def wrapper(context, *args, **kwargs):
        n = context.tree_node
        inference_state = context.inference_state
        try:
            inference_state.inferred_element_counts[n] += 1
            if inference_state.inferred_element_counts[n] > 300:
                debug.warning('In value %s there were too many inferences.', n)
                return NO_VALUES
        except KeyError:
            inference_state.inferred_element_counts[n] = 1
        return func(context, *args, **kwargs)

    return wrapper


def _py__stop_iteration_returns(generators):
    results = NO_VALUES
    for generator in generators:
        try:
            method = generator.py__stop_iteration_returns
        except AttributeError:
            debug.warning('%s is not actually a generator', generator)
        else:
            results |= method()
    return results


@debug.increase_indent
@_limit_value_infers
def infer_node(context, element):
    debug.dbg('infer_node %s@%s in %s', element, element.start_pos, context)
    inference_state = context.inference_state
    typ = element.type
    if typ in ('name', 'number', 'string', 'atom', 'strings', 'keyword', 'fstring'):
        return infer_atom(context, element)
    elif typ == 'lambdef':
        return ValueSet([FunctionValue.from_value(context, element)])
    elif typ == 'expr_stmt':
        return infer_expr_stmt(context, element)
    elif typ in ('power', 'atom_expr'):
        first_child = element.children[0]
        children = element.children[1:]
        had_await = False
        if first_child.type == 'keyword' and first_child.value == 'await':
            had_await = True
            first_child = children.pop(0)

        value_set = context.infer_node(first_child)
        for (i, trailer) in enumerate(children):
            if trailer == '**':  # has a power operation.
                right = context.infer_node(children[i + 1])
                value_set = _infer_comparison(
                    context,
                    value_set,
                    trailer,
                    right
                )
                break
            value_set = infer_trailer(context, value_set, trailer)

        if had_await:
            return value_set.py__await__().py__stop_iteration_returns()
        return value_set
    elif typ in ('testlist_star_expr', 'testlist',):
        # The implicit tuple in statements.
        return ValueSet([iterable.SequenceLiteralValue(inference_state, context, element)])
    elif typ in ('not_test', 'factor'):
        value_set = context.infer_node(element.children[-1])
        for operator in element.children[:-1]:
            value_set = infer_factor(value_set, operator)
        return value_set
    elif typ == 'test':
        # `x if foo else y` case.
        return (context.infer_node(element.children[0]) |
                context.infer_node(element.children[-1]))
    elif typ == 'operator':
        # Must be an ellipsis, other operators are not inferred.
        # In Python 2 ellipsis is coded as three single dot tokens, not
        # as one token 3 dot token.
        if element.value not in ('.', '...'):
            origin = element.parent
            raise AssertionError("unhandled operator %s in %s " % (repr(element.value), origin))
        return ValueSet([compiled.builtin_from_name(inference_state, u'Ellipsis')])
    elif typ == 'dotted_name':
        value_set = infer_atom(context, element.children[0])
        for next_name in element.children[2::2]:
            # TODO add search_global=True?
            value_set = value_set.py__getattribute__(next_name, name_value=context)
        return value_set
    elif typ == 'eval_input':
        return infer_node(context, element.children[0])
    elif typ == 'annassign':
        return annotation.infer_annotation(context, element.children[1]) \
            .execute_annotation()
    elif typ == 'yield_expr':
        if len(element.children) and element.children[1].type == 'yield_arg':
            # Implies that it's a yield from.
            element = element.children[1].children[1]
            generators = context.infer_node(element) \
                .py__getattribute__('__iter__').execute_with_values()
            return generators.py__stop_iteration_returns()

        # Generator.send() is not implemented.
        return NO_VALUES
    elif typ == 'namedexpr_test':
        return infer_node(context, element.children[2])
    else:
        return infer_or_test(context, element)


def infer_trailer(context, atom_values, trailer):
    trailer_op, node = trailer.children[:2]
    if node == ')':  # `arglist` is optional.
        node = None

    if trailer_op == '[':
        trailer_op, node, _ = trailer.children
        return atom_values.get_item(
            _infer_subscript_list(context, node),
            ValueualizedNode(context, trailer)
        )
    else:
        debug.dbg('infer_trailer: %s in %s', trailer, atom_values)
        if trailer_op == '.':
            return atom_values.py__getattribute__(
                name_context=context,
                name_or_str=node
            )
        else:
            assert trailer_op == '(', 'trailer_op is actually %s' % trailer_op
            args = arguments.TreeArguments(context.inference_state, context, node, trailer)
            return atom_values.execute(args)


def infer_atom(context, atom):
    """
    Basically to process ``atom`` nodes. The parser sometimes doesn't
    generate the node (because it has just one child). In that case an atom
    might be a name or a literal as well.
    """
    state = context.inference_state
    if atom.type == 'name':
        if atom.value in ('True', 'False', 'None'):
            # Python 2...
            return ValueSet([compiled.builtin_from_name(state, atom.value)])

        # This is the first global lookup.
        stmt = tree.search_ancestor(
            atom, 'expr_stmt', 'lambdef'
        ) or atom
        if stmt.type == 'lambdef':
            stmt = atom
        position = stmt.start_pos
        if _is_annotation_name(atom):
            # Since Python 3.7 (with from __future__ import annotations),
            # annotations are essentially strings and can reference objects
            # that are defined further down in code. Therefore just set the
            # position to None, so the finder will not try to stop at a certain
            # position in the module.
            position = None
        return context.py__getattribute__(
            name_or_str=atom,
            position=position,
            search_global=True
        )
    elif atom.type == 'keyword':
        # For False/True/None
        if atom.value in ('False', 'True', 'None'):
            return ValueSet([compiled.builtin_from_name(state, atom.value)])
        elif atom.value == 'print':
            # print e.g. could be inferred like this in Python 2.7
            return NO_VALUES
        elif atom.value == 'yield':
            # Contrary to yield from, yield can just appear alone to return a
            # value when used with `.send()`.
            return NO_VALUES
        assert False, 'Cannot infer the keyword %s' % atom

    elif isinstance(atom, tree.Literal):
        string = state.compiled_subprocess.safe_literal_eval(atom.value)
        return ValueSet([compiled.create_simple_object(state, string)])
    elif atom.type == 'strings':
        # Will be multiple string.
        value_set = infer_atom(context, atom.children[0])
        for string in atom.children[1:]:
            right = infer_atom(context, string)
            value_set = _infer_comparison(state, value_set, u'+', right)
        return value_set
    elif atom.type == 'fstring':
        return compiled.get_string_value_set(state)
    else:
        c = atom.children
        # Parentheses without commas are not tuples.
        if c[0] == '(' and not len(c) == 2 \
                and not(c[1].type == 'testlist_comp' and
                        len(c[1].children) > 1):
            return context.infer_node(c[1])

        try:
            comp_for = c[1].children[1]
        except (IndexError, AttributeError):
            pass
        else:
            if comp_for == ':':
                # Dict comprehensions have a colon at the 3rd index.
                try:
                    comp_for = c[1].children[3]
                except IndexError:
                    pass

            if comp_for.type in ('comp_for', 'sync_comp_for'):
                return ValueSet([iterable.comprehension_from_atom(
                    state, context, atom
                )])

        # It's a dict/list/tuple literal.
        array_node = c[1]
        try:
            array_node_c = array_node.children
        except AttributeError:
            array_node_c = []
        if c[0] == '{' and (array_node == '}' or ':' in array_node_c or
                            '**' in array_node_c):
            new_value = iterable.DictLiteralValue(state, context, atom)
        else:
            new_value = iterable.SequenceLiteralValue(state, context, atom)
        return ValueSet([new_value])


@_limit_value_infers
def infer_expr_stmt(context, stmt, seek_name=None):
    with recursion.execution_allowed(context.inference_state, stmt) as allowed:
        # Here we allow list/set to recurse under certain conditions. To make
        # it possible to resolve stuff like list(set(list(x))), this is
        # necessary.
        if not allowed and context.get_root_value() == context.inference_state.builtins_module:
            try:
                instance = context.var_args.instance
            except AttributeError:
                pass
            else:
                if instance.name.string_name in ('list', 'set'):
                    c = instance.get_first_non_keyword_argument_values()
                    if instance not in c:
                        allowed = True

        if allowed:
            return _infer_expr_stmt(context, stmt, seek_name)
    return NO_VALUES


@debug.increase_indent
def _infer_expr_stmt(context, stmt, seek_name=None):
    """
    The starting point of the completion. A statement always owns a call
    list, which are the calls, that a statement does. In case multiple
    names are defined in the statement, `seek_name` returns the result for
    this name.

    :param stmt: A `tree.ExprStmt`.
    """
    debug.dbg('infer_expr_stmt %s (%s)', stmt, seek_name)
    rhs = stmt.get_rhs()
    value_set = context.infer_node(rhs)

    if seek_name:
        c_node = ValueualizedName(context, seek_name)
        value_set = check_tuple_assignments(c_node, value_set)

    first_operator = next(stmt.yield_operators(), None)
    if first_operator not in ('=', None) and first_operator.type == 'operator':
        # `=` is always the last character in aug assignments -> -1
        operator = copy.copy(first_operator)
        operator.value = operator.value[:-1]
        name = stmt.get_defined_names()[0].value
        left = context.py__getattribute__(
            name, position=stmt.start_pos, search_global=True)

        for_stmt = tree.search_ancestor(stmt, 'for_stmt')
        if for_stmt is not None and for_stmt.type == 'for_stmt' and value_set \
                and parser_utils.for_stmt_defines_one_name(for_stmt):
            # Iterate through result and add the values, that's possible
            # only in for loops without clutter, because they are
            # predictable. Also only do it, if the variable is not a tuple.
            node = for_stmt.get_testlist()
            cn = ValueualizedNode(context, node)
            ordered = list(cn.infer().iterate(cn))

            for lazy_value in ordered:
                dct = {for_stmt.children[1].value: lazy_value.infer()}
                with helpers.predefine_names(context, for_stmt, dct):
                    t = context.infer_node(rhs)
                    left = _infer_comparison(context, left, operator, t)
            value_set = left
        else:
            value_set = _infer_comparison(context, left, operator, value_set)
    debug.dbg('infer_expr_stmt result %s', value_set)
    return value_set


def infer_or_test(context, or_test):
    iterator = iter(or_test.children)
    types = context.infer_node(next(iterator))
    for operator in iterator:
        right = next(iterator)
        if operator.type == 'comp_op':  # not in / is not
            operator = ' '.join(c.value for c in operator.children)

        # handle type inference of and/or here.
        if operator in ('and', 'or'):
            left_bools = set(left.py__bool__() for left in types)
            if left_bools == {True}:
                if operator == 'and':
                    types = context.infer_node(right)
            elif left_bools == {False}:
                if operator != 'and':
                    types = context.infer_node(right)
            # Otherwise continue, because of uncertainty.
        else:
            types = _infer_comparison(context, types, operator,
                                      context.infer_node(right))
    debug.dbg('infer_or_test types %s', types)
    return types


@iterator_to_value_set
def infer_factor(value_set, operator):
    """
    Calculates `+`, `-`, `~` and `not` prefixes.
    """
    for value in value_set:
        if operator == '-':
            if is_number(value):
                yield value.negate()
        elif operator == 'not':
            b = value.py__bool__()
            if b is None:  # Uncertainty.
                return
            yield compiled.create_simple_object(value.inference_state, not b)
        else:
            yield value


def _literals_to_types(inference_state, result):
    # Changes literals ('a', 1, 1.0, etc) to its type instances (str(),
    # int(), float(), etc).
    new_result = NO_VALUES
    for typ in result:
        if is_literal(typ):
            # Literals are only valid as long as the operations are
            # correct. Otherwise add a value-free instance.
            cls = compiled.builtin_from_name(inference_state, typ.name.string_name)
            new_result |= cls.execute_with_values()
        else:
            new_result |= ValueSet([typ])
    return new_result


def _infer_comparison(context, left_values, operator, right_values):
    state = context.inference_state
    if not left_values or not right_values:
        # illegal slices e.g. cause left/right_result to be None
        result = (left_values or NO_VALUES) | (right_values or NO_VALUES)
        return _literals_to_types(state, result)
    else:
        # I don't think there's a reasonable chance that a string
        # operation is still correct, once we pass something like six
        # objects.
        if len(left_values) * len(right_values) > 6:
            return _literals_to_types(state, left_values | right_values)
        else:
            return ValueSet.from_sets(
                _infer_comparison_part(state, context, left, operator, right)
                for left in left_values
                for right in right_values
            )


def _is_annotation_name(name):
    ancestor = tree.search_ancestor(name, 'param', 'funcdef', 'expr_stmt')
    if ancestor is None:
        return False

    if ancestor.type in ('param', 'funcdef'):
        ann = ancestor.annotation
        if ann is not None:
            return ann.start_pos <= name.start_pos < ann.end_pos
    elif ancestor.type == 'expr_stmt':
        c = ancestor.children
        if len(c) > 1 and c[1].type == 'annassign':
            return c[1].start_pos <= name.start_pos < c[1].end_pos
    return False


def _is_tuple(value):
    return isinstance(value, iterable.Sequence) and value.array_type == 'tuple'


def _is_list(value):
    return isinstance(value, iterable.Sequence) and value.array_type == 'list'


def _bool_to_value(inference_state, bool_):
    return compiled.builtin_from_name(inference_state, force_unicode(str(bool_)))


def _get_tuple_ints(value):
    if not isinstance(value, iterable.SequenceLiteralValue):
        return None
    numbers = []
    for lazy_value in value.py__iter__():
        if not isinstance(lazy_value, LazyTreeValue):
            return None
        node = lazy_value.data
        if node.type != 'number':
            return None
        try:
            numbers.append(int(node.value))
        except ValueError:
            return None
    return numbers


def _infer_comparison_part(inference_state, context, left, operator, right):
    l_is_num = is_number(left)
    r_is_num = is_number(right)
    if isinstance(operator, unicode):
        str_operator = operator
    else:
        str_operator = force_unicode(str(operator.value))

    if str_operator == '*':
        # for iterables, ignore * operations
        if isinstance(left, iterable.Sequence) or is_string(left):
            return ValueSet([left])
        elif isinstance(right, iterable.Sequence) or is_string(right):
            return ValueSet([right])
    elif str_operator == '+':
        if l_is_num and r_is_num or is_string(left) and is_string(right):
            return ValueSet([left.execute_operation(right, str_operator)])
        elif _is_tuple(left) and _is_tuple(right) or _is_list(left) and _is_list(right):
            return ValueSet([iterable.MergedArray(inference_state, (left, right))])
    elif str_operator == '-':
        if l_is_num and r_is_num:
            return ValueSet([left.execute_operation(right, str_operator)])
    elif str_operator == '%':
        # With strings and numbers the left type typically remains. Except for
        # `int() % float()`.
        return ValueSet([left])
    elif str_operator in COMPARISON_OPERATORS:
        if left.is_compiled() and right.is_compiled():
            # Possible, because the return is not an option. Just compare.
            try:
                return ValueSet([left.execute_operation(right, str_operator)])
            except TypeError:
                # Could be True or False.
                pass
        else:
            if str_operator in ('is', '!=', '==', 'is not'):
                operation = COMPARISON_OPERATORS[str_operator]
                bool_ = operation(left, right)
                return ValueSet([_bool_to_value(inference_state, bool_)])

            if isinstance(left, VersionInfo):
                version_info = _get_tuple_ints(right)
                if version_info is not None:
                    bool_result = compiled.access.COMPARISON_OPERATORS[operator](
                        inference_state.environment.version_info,
                        tuple(version_info)
                    )
                    return ValueSet([_bool_to_value(inference_state, bool_result)])

        return ValueSet([
            _bool_to_value(inference_state, True),
            _bool_to_value(inference_state, False)
        ])
    elif str_operator == 'in':
        return NO_VALUES

    def check(obj):
        """Checks if a Jedi object is either a float or an int."""
        return isinstance(obj, TreeInstance) and \
            obj.name.string_name in ('int', 'float')

    # Static analysis, one is a number, the other one is not.
    if str_operator in ('+', '-') and l_is_num != r_is_num \
            and not (check(left) or check(right)):
        message = "TypeError: unsupported operand type(s) for +: %s and %s"
        analysis.add(context, 'type-error-operation', operator,
                     message % (left, right))

    result = ValueSet([left, right])
    debug.dbg('Used operator %s resulting in %s', operator, result)
    return result


def _remove_statements(context, stmt, name):
    """
    This is the part where statements are being stripped.

    Due to lazy type inference, statements like a = func; b = a; b() have to be
    inferred.

    TODO merge with infer_expr_stmt?
    """
    pep0484_values = \
        annotation.find_type_from_comment_hint_assign(context, stmt, name)
    if pep0484_values:
        return pep0484_values

    return infer_expr_stmt(context, stmt, seek_name=name)


@plugin_manager.decorate()
def tree_name_to_values(inference_state, context, tree_name):
    value_set = NO_VALUES
    module_node = context.get_root_context().tree_node
    # First check for annotations, like: `foo: int = 3`
    if module_node is not None:
        names = module_node.get_used_names().get(tree_name.value, [])
        for name in names:
            expr_stmt = name.parent

            if expr_stmt.type == "expr_stmt" and expr_stmt.children[1].type == "annassign":
                correct_scope = parser_utils.get_parent_scope(name) == context.tree_node
                if correct_scope:
                    value_set |= annotation.infer_annotation(
                        context, expr_stmt.children[1].children[1]
                    ).execute_annotation()
        if value_set:
            return value_set

    types = []
    node = tree_name.get_definition(import_name_always=True)
    if node is None:
        node = tree_name.parent
        if node.type == 'global_stmt':
            c = context.create_context(tree_name)
            raise NotImplementedError
            finder = NameFinder(inference_state, value, value, tree_name.value)
            filters = finder.get_global_filters()
            # For global_stmt lookups, we only need the first possible scope,
            # which means the function itself.
            filters = [next(filters)]
            return finder.find(filters, attribute_lookup=False)
        elif node.type not in ('import_from', 'import_name'):
            c = inference_state.create_context(context, tree_name)
            return infer_atom(c, tree_name)

    typ = node.type
    if typ == 'for_stmt':
        types = annotation.find_type_from_comment_hint_for(context, node, tree_name)
        if types:
            return types
    if typ == 'with_stmt':
        types = annotation.find_type_from_comment_hint_with(context, node, tree_name)
        if types:
            return types

    if typ in ('for_stmt', 'comp_for', 'sync_comp_for'):
        try:
            types = context.predefined_names[node][tree_name.value]
        except KeyError:
            cn = ValueualizedNode(context, node.children[3])
            for_types = iterate_values(
                cn.infer(),
                valueualized_node=cn,
                is_async=node.parent.type == 'async_stmt',
            )
            c_node = ValueualizedName(context, tree_name)
            types = check_tuple_assignments(c_node, for_types)
    elif typ == 'expr_stmt':
        types = _remove_statements(context, node, tree_name)
    elif typ == 'with_stmt':
        value_managers = context.infer_node(node.get_test_node_from_name(tree_name))
        enter_methods = value_managers.py__getattribute__(u'__enter__')
        return enter_methods.execute_with_values()
    elif typ in ('import_from', 'import_name'):
        types = imports.infer_import(context, tree_name)
    elif typ in ('funcdef', 'classdef'):
        types = _apply_decorators(context, node)
    elif typ == 'try_stmt':
        # TODO an exception can also be a tuple. Check for those.
        # TODO check for types that are not classes and add it to
        # the static analysis report.
        exceptions = context.infer_node(tree_name.get_previous_sibling().get_previous_sibling())
        types = exceptions.execute_with_values()
    elif node.type == 'param':
        types = NO_VALUES
    else:
        raise ValueError("Should not happen. type: %s" % typ)
    return types


# We don't want to have functions/classes that are created by the same
# tree_node.
@inference_state_method_cache()
def _apply_decorators(context, node):
    """
    Returns the function, that should to be executed in the end.
    This is also the places where the decorators are processed.
    """
    if node.type == 'classdef':
        decoratee_value = ClassValue(
            context.inference_state,
            parent_context=context,
            tree_node=node
        )
    else:
        decoratee_value = FunctionValue.from_value(context, node)
    initial = values = ValueSet([decoratee_value])
    for dec in reversed(node.get_decorators()):
        debug.dbg('decorator: %s %s', dec, values, color="MAGENTA")
        with debug.increase_indent_cm():
            dec_values = context.infer_node(dec.children[1])
            trailer_nodes = dec.children[2:-1]
            if trailer_nodes:
                # Create a trailer and infer it.
                trailer = tree.PythonNode('trailer', trailer_nodes)
                trailer.parent = dec
                dec_values = infer_trailer(context, dec_values, trailer)

            if not len(dec_values):
                code = dec.get_code(include_prefix=False)
                # For the short future, we don't want to hear about the runtime
                # decorator in typing that was intentionally omitted. This is not
                # "correct", but helps with debugging.
                if code != '@runtime\n':
                    debug.warning('decorator not found: %s on %s', dec, node)
                return initial

            values = dec_values.execute(arguments.ValuesArguments([values]))
            if not len(values):
                debug.warning('not possible to resolve wrappers found %s', node)
                return initial

        debug.dbg('decorator end %s', values, color="MAGENTA")
    if values != initial:
        return ValueSet([Decoratee(c, decoratee_value) for c in values])
    return values


def check_tuple_assignments(valueualized_name, value_set):
    """
    Checks if tuples are assigned.
    """
    lazy_value = None
    for index, node in valueualized_name.assignment_indexes():
        cn = ValueualizedNode(valueualized_name.value, node)
        iterated = value_set.iterate(cn)
        if isinstance(index, slice):
            # For no star unpacking is not possible.
            return NO_VALUES
        for _ in range(index + 1):
            try:
                lazy_value = next(iterated)
            except StopIteration:
                # We could do this with the default param in next. But this
                # would allow this loop to run for a very long time if the
                # index number is high. Therefore break if the loop is
                # finished.
                return NO_VALUES
        value_set = lazy_value.infer()
    return value_set


def _infer_subscript_list(context, index):
    """
    Handles slices in subscript nodes.
    """
    if index == ':':
        # Like array[:]
        return ValueSet([iterable.Slice(context, None, None, None)])

    elif index.type == 'subscript' and not index.children[0] == '.':
        # subscript basically implies a slice operation, except for Python 2's
        # Ellipsis.
        # e.g. array[:3]
        result = []
        for el in index.children:
            if el == ':':
                if not result:
                    result.append(None)
            elif el.type == 'sliceop':
                if len(el.children) == 2:
                    result.append(el.children[1])
            else:
                result.append(el)
        result += [None] * (3 - len(result))

        return ValueSet([iterable.Slice(context, *result)])
    elif index.type == 'subscriptlist':
        return ValueSet([iterable.SequenceLiteralValue(context.inference_state, context, index)])

    # No slices
    return context.infer_node(index)
