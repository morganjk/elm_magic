# -*- coding: utf-8 -*-

from collections import deque
import contextlib
import io
import os
import subprocess
import sys
from IPython.core.display import display_javascript
from IPython.core.display import display, Javascript, HTML
from IPython.core.display import clear_output
from IPython.core.magic import (Magics, magics_class, line_magic,
                                cell_magic, line_cell_magic)

from IPython.core.magic_arguments import argument, magic_arguments, parse_argstring

from tempfile import TemporaryDirectory

try:
    from traitlets.config.configurable import Configurable
    from traitlets import Bool, Int, Unicode
except ImportError:
    from IPython.config.configurable import Configurable
    from IPython.utils.traitlets import Bool, Int, Unicode


def _create_success_js(js,div_id):
    """create html with the results of a successful compilation.
    `js` is the javascript generated by elm-make.
    `div_id` is the id of the html element that Elm will embed int
    """
    module_name = "Main"
    template = """
        <div id="{div_id}"></div>
        <script>
            var defineElm = function(cb) {{
                if (this.Elm) {{
                    this.oldElm = this.Elm;
                }}
                var define = null;
                {js}
                cb();
            }}
            ;
            var obj = new Object();
            defineElm.bind(obj)(function(){{
                var mountNode = document.getElementById('{div_id}');
                obj.Elm. {module_name}.embed(mountNode);
            }});
        </script>
    """

    js = template.format(
        js=js,
        module_name=module_name,
        div_id=div_id)
    
    return js

def elm_package_install(package,workdir):
    command = "elm-package install {package} --yes".format(package=package)
    try:
        out = subprocess.check_output(
                 command.split(),
                 cwd=workdir,
                 stderr=subprocess.STDOUT
              )
    except subprocess.CalledProcessError as err:
        raise CompilationFailure("ElmPackageInstallFailed: \n{}".format(err))

    return out
            

@magics_class
class ElmMagic(Magics, Configurable):
    """Compiles elm-lang code and displays result
       as html/js.  It can be configured to use either a temporary
       or user-specified directory for compilation 
    """

    elmdir = Unicode(value='',config=True, help="Directory where elm-make will run. (This is where elm-stuff and elm-package.json will be). Uses a temporary directory if not set by user")

    keep_sources = Bool(False, config=True, help="Do not immediately delete the html/js generated by compilation")

    render = Bool(False, config=True, help="Use elm-static-html to render the cell's contents before displaying")
    
    debug = Bool(False, config=True, help="Display debuging output")

    def __init__(self, shell):
        # You must call the parent constructor
        Configurable.__init__(self,config=shell.config)
        Magics.__init__(self,shell=shell)

        self._tempdir = None
        self._execution_count = 0

        # Add ourself to the list of module configurable via %config
        self.shell.configurables.append(self)

    @property
    def _workdir(self):
        if self.elmdir != '':
            if not os.path.isdir(self.elmdir):
                os.makedirs(self.elmdir)
            return self.elmdir

        if not self._tempdir:
            self._tempdir = TemporaryDirectory()

        return self._tempdir.name

    def _log(self,msg,debug=False):
        if self.debug or debug:
            print(msg)

        
    @magic_arguments()
    @cell_magic
    @argument('-i','--install', type=str, nargs='+',action='append',help="install elm packages")
    @argument('-w','--workdir', type=str,help="location for elm-make to run")
    @argument('-k','--keep', action="store_true",help="dont autodelete elm and js files")
    @argument('-r','--render', action="store_true",help="server-side render html (alpha)")
    @argument('-d','--debug', action="store_true",help="print debug information")
    def elm(self,arg=None, cell=None):
        """Compile and display Elm code"""

        args = parse_argstring(self.elm, arg)

        if args.workdir:
            self.elmdir = args.workdir

        do_keep = args.keep or self.keep_sources

        do_render = args.render or self.render

        if args.install:
            packages = args.install[0]
            for p in packages:
                elm_package_install(p,self._workdir)

        if cell is None:
            print("Please use as cell-magic (use second line)")
            return 

        self._execution_count += 1
        div_id = 'elm-div-'+str(self._execution_count)

        self._log("Compiling Elm in workdir {}".format(self._workdir),args.debug)

        if do_render:
            self._log("Serverside rendering Elm source\n{}".format(cell),args.debug)
            result = ElmRenderer(self._workdir,do_keep).do_compile(cell)
            try:
                display(HTML(result._js))
            except:
                result.display(div_id)
            self._log("Elm result \n{}".format(result._js),args.debug)
            return
        else:
            self._log("Compiling Elm source\n{}".format(cell),args.debug)
            result = ElmCompiler(self._workdir,do_keep).do_compile(cell)

        clear_output
        result.display(div_id)

class CompilationResult:
    pass

class CompilationSuccess(CompilationResult):
    def __init__(self,js):
        self._js = js
        
    def display(self,div_id):        
        js = _create_success_js(self._js,div_id)
        display(HTML(js))

class CompilationNOOP(CompilationResult):
    
    def display(self,div_id):
        display(HTML("<h1 id='{div_id}'>Ok</h1>".format(div_id)))
        
class CompilationFailure(CompilationResult):
    
    def __init__(self,msg):
        self._msg = msg
    
    def display(self,div_id):
        lines = self._msg.split('\n')
        html = "<div id='{}'>".format(div_id) \
               + ' '.join(['<pre>{}</pre>'.format(line) for line in lines]) \
               + "</div>"
        display(HTML(html))
    
class ElmCompiler:
    """Wrapper around elm-make to return compiled JS string from Elm source.  
       By default, removes generated sources immediately after compilation
    """

    def __init__(self, workdir=None, keep_sources=False):
        self._code = []
        self._keep_sources = keep_sources
        self._workdir = workdir
        self._tempdir = None

        if not self._workdir:
            self._tempdir = TemporaryDirectory()
            self._workdir = self._tempdir.name
            
    def do_compile(self, code):
        """ Returns instance of CompilationResult """
        self._code.append(code)
        if self._should_compile:
            try:
                code = "\n".join(self._code)
                self._code = []
                return self._compile(code)
            except Exception as exc:
                return CompilationFailure(str(exc))

        return CompilationNOOP()

    @contextlib.contextmanager
    def _tempfile(self, filename):
        """Yield `filename` inside the workdir, but don't actually create the file.
        Then, on exit, delete the file if it exists.
        """
        try:
            path = os.path.join(self._workdir, filename)
            yield path
        finally:
            with contextlib.suppress(OSError):
                if not self._keep_sources:
                    os.remove(path)

    def _compile(self, code):
        # Try to infer module name from source code "
        module_name = 'Main'
        try:
            if 'module' in code:
                tokens = code.split()
                module_name = tokens[tokens.index('module')+1]
        except Exception as e:
            print("using Main as module name since since I couldn't infer from source {}".format(e))
        
        elm_file = module_name+'.elm'
        js_file = module_name+'.js'

        with self._tempfile(elm_file) as infile,\
             self._tempfile(js_file) as outfile:

            with open(infile, mode='wt') as f:
                f.write(code)

            out = "No Compiler Output"
            try:
                out = self.run_compile_command(self._workdir,infile,outfile)
                
                with open(outfile, mode='rt') as f:
                    javascript = f.read()
                
                return CompilationSuccess(javascript)

            except subprocess.CalledProcessError as err:
                # When compilation fails we send the compiler output to the
                # user but we don't count this as an error. A compiler error
                # might actually be the desired output of the cell.
                return CompilationFailure("CompilationFailed: \n" + err.stdout.decode('UTF-8'))
            except FileNotFoundError as err:
                if err.filename == infile:
                    return CompilationFailure("Could not find input file for compilation: {}. compiler output: \n{}".format(infile,out))
                elif err.filename == outfile:
                    return CompilationFailure("Could not find compiled file after compilation: {} compiler output: \n{}".format(outfile,out))
                raise err
            except Exception as err:
                return CompilationFailure("Unknown exception while compiling: \n" + str(err))

    def run_compile_command(self,workdir,infile,outfile):
            command = "elm-make {infile} --yes --output {outfile}".format(infile=infile,outfile=outfile)
            out = subprocess.check_output(
                command.split(),
                cwd=workdir,
                stderr=subprocess.STDOUT)
            return out.decode('UTF-8')

    @property
    def _should_compile(self):
        """Hook for conditional compilation later. (TODO)"""
        return True
 
class ElmRenderer(ElmCompiler):
    """Wrapper around elm-static-html to return rendered html string from Elm source.  
    """
    def run_compile_command(self,workdir,infile,outfile):
         command = "elm-static-html -f {infile} --output {outfile}".format(infile=infile,outfile=outfile)
         out = subprocess.check_output(
                command.split(),
                cwd=workdir,
                stderr=subprocess.STDOUT)

         # handle special errors for elm-static command
         # elm-static-html returns 0 error code even if output was not generated
         special_error = b"Exited with the code 1"
         if special_error in out:
             errors = out.split(special_error)[0]
             raise subprocess.CalledProcessError(1,command,output=errors)

         return out.decode('UTF-8')

                
def load_ipython_extension(ip):
    """Load the extension in IPython."""
    magics = ElmMagic(ip)
    ip.register_magics(magics)
