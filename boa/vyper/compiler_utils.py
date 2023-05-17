import textwrap

import vyper.ast as vy_ast
import vyper.semantics.analysis as analysis
from vyper.ast.utils import parse_to_ast
from vyper.codegen.function_definitions import generate_ir_for_function
from vyper.codegen.ir_node import IRnode
from vyper.exceptions import InvalidType
from vyper.ir import compile_ir as compile_ir
from vyper.semantics.analysis.utils import get_exact_type_from_node
from vyper.utils import method_id_int

from boa.vyper import _METHOD_ID_VAR


def _compile_vyper_function(vyper_function, contract):
    """Compiles a vyper function and appends it to the top of the IR of a
    contract. This is useful for vyper `eval` and internal functions, where
    the runtime bytecode must be changed to add more runtime functionality
    (such as eval, and calling internal functions)
    """

    compiler_data = contract.compiler_data
    global_ctx = contract.global_ctx
    ifaces = compiler_data.interface_codes
    ast = parse_to_ast(vyper_function, ifaces)

    # override namespace and add wrapper code at the top
    with contract.override_vyper_namespace():
        analysis.add_module_namespace(ast, ifaces)
        analysis.validate_functions(ast)

    ast = ast.body[0]
    ir = generate_ir_for_function(ast, global_ctx, False)

    ir = IRnode.from_list(
        ["with", _METHOD_ID_VAR, method_id_int(sig.base_signature), ir]
    )
    assembly = compile_ir.compile_to_assembly(ir, no_optimize=True)

    # extend IR with contract's unoptimized assembly
    assembly.extend(contract.unoptimized_assembly)
    compile_ir._optimize_assembly(assembly)
    bytecode, source_map = compile_ir.assembly_to_evm(assembly)
    bytecode += contract.data_section
    typ = sig.return_type

    return ast, bytecode, source_map, typ


def generate_bytecode_for_internal_fn(fn):
    """Wraps internal fns with an external fn and generated bytecode"""

    contract = fn.contract
    fn_name = fn.fn_signature.name
    fn_args = ", ".join([arg.name for arg in fn.fn_signature.arguments])

    return_sig = ""
    fn_call = ""
    if fn.fn_signature.return_type:
        return_sig = f" -> {fn.fn_signature.return_type}"
        fn_call = "return "
    fn_call += f"self.{fn_name}({fn_args})"

    # same but with defaults, signatures, etc.:
    _fn_sig = []
    for arg in fn.fn_signature.arguments:
        sig_arg_text = f"{arg.name}: {arg.typ}"

        # check if arg has a default value:
        if arg.name in fn.fn_signature.default_values:
            default_value = fn.fn_signature.default_values[arg.name].value
            sig_arg_text += f" = {default_value}"

        _fn_sig.append(sig_arg_text)
    fn_sig = ", ".join(_fn_sig)

    wrapper_code = textwrap.dedent(
        f"""
        @external
        @payable
        def __boa_private_{fn_name}__({fn_sig}){return_sig}:
            {fn_call}
    """
    )
    return _compile_vyper_function(wrapper_code, contract)[1:]


def generate_bytecode_for_arbitrary_stmt(source_code, contract):
    """Wraps arbitrary stmts with external fn and generates bytecode"""

    ast = parse_to_ast(source_code)
    vy_ast.folding.fold(ast)
    ast = ast.body[0]

    return_sig = ""
    debug_body = source_code

    if isinstance(ast, vy_ast.Expr):
        with contract.override_vyper_namespace():
            try:
                ast_typ = get_exact_type_from_node(ast.value)
                return_sig = f"-> {ast_typ}"
                debug_body = f"return {source_code}"
            except InvalidType:
                pass

    # wrap code in function so that we can easily generate code for it
    wrapper_code = textwrap.dedent(
        f"""
        @external
        @payable
        def __boa_debug__() {return_sig}:
            {debug_body}
    """
    )
    return _compile_vyper_function(wrapper_code, contract)[1:]
