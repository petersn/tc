#!/usr/bin/python

import time, os, tempfile, struct

DEBUG_MODE = False

asm_template = """; Assembly generated by tc at %(date)s

%(externs)s

; This all important constant is made available to the user.
WORDSIZE equ 8

section .data
	scratch: dq 0
	TracebackStackPointer: dq scratch
%(data)s

section .text
	global main
main:
%(main_code)s

	; Exit cleanly.
	mov rax, 60
	mov rdi, 0
	syscall
"""

escape_table = {
	"\\": "\\",
	"n": "\n",
	"t": "\t",
	"0": "\0",
}

search_path = [".", "/home/snp/proj/tc/lib"]

tag_counter = 0
def get_tag():
	global tag_counter
	tag_counter += 1
	return "tag%i" % tag_counter

class Extern:
	calling_convention = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]

	def __init__(self, name, num_args, does_return):
		self.name, self.num_args, self.does_return = name, num_args, does_return

	def make_decl(self):
		return ["extern %s" % self.name]

	def call(self, arg_count=None):
		if arg_count != None and self.num_args != "variable":
			raise Exception("Trying to set number of arguments on non-variadic extern.")
		c = []
		for reg in self.calling_convention[:self.num_args if self.num_args != "variable" else arg_count][::-1]:
			c.append("pop %s" % reg)
		c.append("push rbp")
		c.append("call %s" % self.name)
		c.append("pop rbp")
		if self.does_return:
			c.append("push rax")
		return c

class Function:
	def __init__(self, name):
		self.name = name
		self.local = []
		self.args = []
		self.start_tag = get_tag()
		self.return_tag = get_tag()
		self.end_tag = get_tag()

	def build_preamble(self):
		bytes_of_vars = 8 + len(self.local)*8
		core = [
			"%s:" % self.start_tag,
			# Write that we're being called into the traceback stack.
			"mov rax, [TracebackStackPointer]",
			"add rax, 8",
			"mov qword [rax], %s" % new_string(self.name),
			"add qword [TracebackStackPointer], 8",
			# Make room on our calling stack.
			"pop qword [r15+8]",
			"add r15, %i" % bytes_of_vars,
		]
		# Pop args into our locals.
		for arg in self.args[::-1]:
			core.extend(self.assign(arg))
		return core

	def access(self, name):
		offset = self.local.index(name) * 8
		return ["push qword [r15-%i]" % offset]

	def assign(self, name):
		assert name in self.local, "Variable not in context: %r" % name
		offset = self.local.index(name) * 8
		return ["pop qword [r15-%i]" % offset]

	def call(self):
		return ["call %s" % self.start_tag]

predef = {
	"+": ["pop rax", "add [rsp], rax"],
	"-": ["pop rax", "sub [rsp], rax"],
	"*": ["pop rax", "imul qword [rsp]", "mov [rsp], rax"],
	"/": ["pop rbx", "pop rax", "xor rdx, rdx", "idiv rbx", "push rax"],
	"%": ["pop rbx", "pop rax", "xor rdx, rdx", "idiv rbx", "push rdx"],
	"@": ["pop rax", "pop rbx", "push qword [rax+rbx*8]"],
	"@=": ["pop rax", "pop rbx", "pop qword [rax+rbx*8]"],
	"@c": ["pop rax", "pop rbx", "xor rcx, rcx", "mov cl, [rax+rbx]", "push rcx"],
	"@c=": ["pop rax", "pop rbx", "pop rcx", "mov [rax+rbx], cl"],
	"divmod": ["pop rbx", "pop rax", "xor rdx, rdx", "idiv rbx", "push rax", "push rdx"],
	"drop": ["pop rax"],
	"dup": ["push qword [rsp]"],
	"swap": ["pop rax", "pop rbx", "push rax", "push rbx"],
	"WORDSIZE": ["push 8"],
}

comparison_mapping = {
	"<": "jnl",
	">": "jng",
	"==": "jne",
	"!=": "je",
	"<=": "jnle",
	">=": "jnge",
}

def format_code(seq, indentation=0):
	return "\n".join(indentation*"\t" + line for line in seq)

def format_string_def(i, s):
	tag = "string%i" % i
	s += "\0"
	return "%s: db %s" % (tag, ",".join(hex(ord(c)) for c in s))

def get_include_code(path):
	for directory in search_path:
		p = os.path.join(directory, path)
		if os.path.exists(p):
			with open(p) as f:
				return f.read()
	raise Exception("Couldn't find include: %r" % path)

class FlowContext:
	def __init__(self, code, break_point=None, continue_point=None):
		self.code, self.break_point, self.continue_point = code, break_point, continue_point

whitespace = " \t\n"

def new_string(string):
	if string not in strings:
		strings.append(string)
	return "string%i" % strings.index(string)

def build(code):
	global strings
	externs = {}
	functions = {}
	most_recent_function = None
	strings = []
	context = []
	flow_stack = []
	def get_from_flow(name):
		for flow in flow_stack[::-1]:
			v = getattr(flow, name)
			if v != None:
				return v
		raise Exception("Concept %s out of place." % name)
	style_cstring = None
	token, mode = "", "main"
	code_index = -1
	code = list(code + " ")
	while code_index < len(code)-1:
		code_index += 1
		c = code[code_index]
		if mode == "main" and c in whitespace:
			if token == "":
				continue
			context.append(";                 %s" % token)
			if DEBUG_MODE:
				context.extend([
					"mov r14, [TracebackStackPointer]",
					"mov qword [r14], %s" % new_string(token),
				])
			if token.startswith("extern:"):
				_, name, num_args, does_return = token.split(":", 3)
				externs[name] = Extern(name, int(num_args) if num_args != "*" else "variable", bool(int(does_return)))
			elif token.startswith("include:"):
				path = token.split(":", 1)[1]
				new_code = get_include_code(path)
				code[code_index+1:code_index+1] = list(new_code)
			elif token.startswith("function:"):
				_, name = token.split(":", 1)
				most_recent_function = functions[name] = Function(name)
				context.extend([
					"; %s %s %s" % ("="*10, name, "="*10),
					"jmp %s" % most_recent_function.end_tag,
				])
			elif token.startswith("var:"):
				_, name = token.split(":", 1)
				most_recent_function.local.append(name)
			elif token.startswith("arg:"):
				_, name = token.split(":", 1)
				most_recent_function.local.append(name)
				most_recent_function.args.append(name)
			elif token == "if":
				t = get_tag()
				context.extend([
					"pop rax",
					"cmp rax, 0",
					"je %s" % t,
				])
				flow_stack.append(FlowContext([
					"%s:" % t,
				]))
			elif token == "else":
				t = get_tag()
				context.extend([
					"jmp %s" % t,
				])
				# Then pop the top thing off, and replace it.
				context.extend(flow_stack.pop().code)
				flow_stack.append(FlowContext([
					"%s:" % t,
				]))
			elif token == "loop":
				t1, t2 = get_tag(), get_tag()
				context.append("%s:" % t1)
				flow_stack.append(FlowContext([
					"jmp %s" % t1,
					"%s:" % t2,
				], continue_point=t1, break_point=t2))
			elif token == "while":
				context.extend([
					"pop rax",
					"cmp rax, 0",
					"je %s" % get_from_flow("break_point"),
				])
			elif token == "continue":
				context.append("jmp %s" % get_from_flow("continue_point"))
			elif token == "break":
				context.append("jmp %s" % get_from_flow("break_point"))
			elif token == "end":
				context.extend(flow_stack.pop().code)
			elif token.startswith("endvars"):
				# Now we can compute the total number of locals.
				context.extend(most_recent_function.build_preamble())
			elif token == "return":
				context.append("jmp %s" % most_recent_function.return_tag)
			elif token == "endfunc":
				context.extend([
					most_recent_function.return_tag + ":",
					"sub qword [TracebackStackPointer], 8",
					"sub r15, %i" % (8 + len(most_recent_function.local)*8),
					"push qword [r15+8]",
					"ret",
					most_recent_function.end_tag + ":",
				])
				most_recent_function = None
			elif token.startswith(":"):
				name = token[1:]
				arg_count = None
				if token.count(":") == 2:
					name, arg_count = name.rsplit(":", 1)
					arg_count = int(arg_count)
				if name in externs:
					context.extend(externs[name].call(arg_count))
				elif name in functions:
					context.extend(functions[name].call())
				else:
					raise Exception("Unknown function/extern: %r" % name)
			elif most_recent_function != None and token in most_recent_function.local:
				context.extend(most_recent_function.access(token))
			elif token in comparison_mapping:
				t = get_tag()
				comp = comparison_mapping[token]
				context.extend(["pop rax", "pop rbx", "xor rcx, rcx", "cmp rbx, rax", "%s %s" % (comp, t), "inc rcx", "%s:" % t, "push rcx"])
			elif token.startswith("="):
				name = token[1:]
				context.extend(most_recent_function.assign(name))
			elif token == ">>>":
				mode = "asm"
				context.append("")
			elif token in predef:
				context.extend(predef[token])
			else:
				try:
					i = int(token)
					context.append("push %i" % i)
				except ValueError:
					raise Exception("Bad token: %r" % token)
			token = ""
		elif mode == "main" and c == "#":
			mode = "comment"
		elif mode == "comment" and c == "\n":
			mode = "main"
		elif mode == "main":
			if (c == '"' or c == "'") and token == "":
				mode = "string"
				string = ""
				style_cstring = c == "'"
				continue
			token += c
		elif mode == "string":
			if (c == '"' and not style_cstring) or (c == "'" and style_cstring):
				assert token == ""
				mode = "main"
				# If we're not a cstring, then add length.
				if not style_cstring:
					string = struct.pack("<Q", len(string)) + string
				context.append("push %s" % new_string(string))
			elif c == "\\":
				mode = "string-escape"
			else:
				string += c
		elif mode == "string-escape":
			string += escape_table.get(c, c)
			mode = "string"
		elif mode == "asm":
			if c == "\n":
				mode = "main"
			else:
				context[-1] += c
#		else: assert False, "In mode: %r with character %r" % (mode, c)

	# Actually produce outputs.
#	root = tempfile.mkdtemp()
#	temp_asm = os.path.join(root, "output.asm")
#	with open(temp_asm, "w") as f:
#		print >>f, "\n".join(context)
	externs = format_code(sum([extern.make_decl() for extern in externs.values()], []), 1)
	data = format_code([format_string_def(i, string) for i, string in enumerate(strings)], 1)
	main_code = format_code(context, 1)
	s = asm_template % {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "externs": externs, "data": data, "main_code": main_code}
	with open("/tmp/code.asm", "w") as f:
		print >>f, s
	os.system("nasm -f elf64 /tmp/code.asm")
	os.system("gcc /tmp/code.o -o /tmp/code")

"""
include:default.tc

function:divmod
	arg:x arg:y
endvars
	x y /
	x y %
endfunc

function:ord
	arg:x
endvars
	0 x @c
endfunc

function:main
	var:x
endvars
	0 =x
	loop
		x 10 <= while
		"Yay" :puts
		x 1 + =x
	end
endfunc

:main
"""

if __name__ == "__main__":
	import sys
	assert len(sys.argv) == 2, "Usage: %s <input.tc>" % sys.argv[0]
	with open(sys.argv[1]) as f:
		data = f.read()
	build(data)

