import time
import json
import re
import logging
import six

import paramiko

logger = logging.getLogger(__name__)

def displaycmd(l):
    s=""
    for t in l:
        p = t.find("[[private:")
        while p != -1:
            q = t.find("]]", p)
            if q != -1:
                t = t[0:p] + t[q+2:]
            else:
                t = t[0:p]
            p = t.find("[[private:", p)
        if s != "":
            s += " "
        if re.search(r"[ \"!{};$]",t):
            s += chr(39)+t+chr(39)
        else:
            s += t
    return s

def rmprivate(l):
    if isinstance(l, list):
        r=[]
        for t in l:
            r.append(rmprivate(t))
        return r
    else:
        t = l
        p = t.find("[[private:")
        while p != -1:
            t = t[0:p] + t[p+10:]
            q = t.find("]]")
            if q != -1:
                t = t[0:q] + t[q+2:]
                p = t.find("[[private:", q)
            else:
                p = -1
        return t

python_run = """
import subprocess
from threading import Thread, Lock
import time
import re
import os

task_id = "%s"
cmd = \"\"\"
%s
\"\"\".strip().split("\\n")
log_file = "%s"
callback_url = "%s"
myenv = %s

def displaycmd(l):
    s=""
    for t in l:
        p = t.find("[[private:")
        while p != -1:
            q = t.find("]]", p)
            if q != -1:
                t = t[0:p+9] + t[q:]
            else:
                t = t[0:p+9]
            p = t.find("[[private:", p+11)
        if s != "":
            s += " "
        if re.search(r"[ \\"!{};$]",t):
            s += chr(39)+t+chr(39)
        else:
            s += t
    return s

def rmprivate(l):
    r=[]
    for t in l:
        p = t.find("[[private:")
        while p != -1:
            t = t[0:p] + t[p+10:]
            q = t.find("]]")
            if q != -1:
                t = t[0:q] + t[q+2:]
                p = t.find("[[private:", q)
            else:
                p = -1
        r.append(t)
    return r

f = open(log_file, "w")
f.write("COMMAND: "+displaycmd(cmd)+"\\n")

p1 = subprocess.Popen(rmprivate(cmd),
                       stdout=subprocess.PIPE, 
                       stderr=subprocess.STDOUT,
                       universal_newlines=True,
                       env=dict(os.environ, **myenv))

current_log = ""

mutex = Lock()
completed = False

def _update_log_loop():
    global current_log
    while True:
        for i in range(60):
            time.sleep(1)
            if completed:
                return
        mutex.acquire()
        copy_log = current_log
        current_log = ""
        mutex.release()
        if copy_log:
            try:
                p = subprocess.Popen(["curl", "-X", "PATCH", callback_url+"/task/log/"+task_id, "--data-binary", "@-"],
                                    stdin=subprocess.PIPE)
                p.communicate(copy_log)
            except Exception:
                pass

if callback_url:
    log_thread = Thread(target=_update_log_loop)
    log_thread.daemon = True
    log_thread.start()

while p1.poll() is None:
    l = p1.stdout.readline()
    f.write(l)
    f.flush()
    mutex.acquire()
    current_log += l
    mutex.release()

completed=True

l = p1.stdout.read()
f.write(l)
f.flush()

if p1.returncode == 0:
    phase = "completed"
else:
    phase = "error"

f.close()

if callback_url:
    mutex.acquire()
    current_log=""
    mutex.release()
    subprocess.call(["curl", "-X", "POST", callback_url+"/task/log/"+task_id, "--data-binary", "@"+log_file])
    subprocess.call(["curl", "-X", "GET", callback_url+"/task/terminate/"+task_id+"?phase=" + phase])
"""

def add_log_handler(fh):
    logger.addHandler(fh)


# Make sure error is processed as binary so won't cause additional exception when decoding
def _patched_exec_command(self, 
                          command, 
                          bufsize=-1, 
                          timeout=None, 
                          get_pty=False, 
                          stdin_binary=False, 
                          stdout_binary=False, 
                          stderr_binary=True):
    
    chan = self._transport.open_session()
    if get_pty:
        chan.get_pty()
    chan.settimeout(timeout)
    chan.exec_command(command)
    stdin = chan.makefile('wb' if stdin_binary else 'w', bufsize)
    stdout = chan.makefile('rb' if stdin_binary else 'r', bufsize)
    stderr = chan.makefile_stderr('rb' if stdin_binary else 'r', bufsize)
    return stdin, stdout, stderr

paramiko.SSHClient.exec_command = _patched_exec_command

def run_command(client, cmd, stdin_content=None, sudo=False, handlePrivate=True):
    if sudo:
        cmd = "sudo " + cmd
    logger.debug("RUN %s", cmd)
    if handlePrivate:
        cmd = rmprivate(cmd)
    stdin, stdout, stderr = client.exec_command(cmd)
    if stdin_content is not None:
        stdin.write(stdin_content)
        stdin.flush()
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, stdout, stderr

def run_docker_command(client, cmd):
    docker_cmd = 'docker %s' % cmd
    return run_command(client, docker_cmd)

def run_and_check_command(client, cmd, stdin_content=None, sudo=False):
    exit_status, _, _ = run_command(
        client, cmd, stdin_content=stdin_content, sudo=sudo)
    return exit_status == 0

def program_exists(client, program):
    return run_and_check_command(client, "command -v %s" % program)

def has_gpu_support(client):
    return run_and_check_command(client, "nvidia-smi")

def ssh_connect_with_retry(hostname,
                           username,
                           key_path,
                           delay=0,
                           retry=0,
                           login_cmd=None):
    """Wrap the SSH connect method with a delay and retry mechanism. This is
    useful when connecting to an instance that was freshly started.
    """
    logger.info("Connecting to %s via SSH...", hostname)
    start = time.time()
    client = paramiko.client.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    while True:
        if delay > 0:
            time.sleep(delay)
        try:
            client.load_system_host_keys()
            client.connect(
                hostname,
                username=username,
                key_filename=key_path,
                look_for_keys=False)
            logger.info("Connection to %s successful (%f)", hostname, time.time()-start)
            if login_cmd is not None:
                if not run_and_check_command(client, login_cmd):
                    raise RuntimeError("failed to run login command")
            return client
        except Exception as e:
            retry -= 1
            if retry < 0:
                raise e
            else:
                logger.warning("Failed to connect to %s via SSH (%s), retrying...",
                               hostname, str(e))

def fuse_s3_bucket(client, corpus):
    if not program_exists(client, "s3fs"):
        raise EnvironmentError("s3fs is not installed")
    if not run_and_check_command(
            client,
            "sed 's/# *user_allow_other/user_allow_other/' -i /etc/fuse.conf",
            sudo=True):
        raise RuntimeError("failed to configure s3fs")
    status, _, stderr = run_command(client, "mkdir -p %s && chmod -R 775 %s" % (
        corpus["mount"], corpus["mount"]))
    if status != 0:
        return RuntimeError('failed to created mount directory: %s' % stderr.read())
    status, _, stderr = run_command(client, "echo %s:%s > s3_passwd && chmod 600 s3_passwd" % (
        corpus["credentials"]["AWS_ACCESS_KEY_ID"],
        corpus["credentials"]["AWS_SECRET_ACCESS_KEY"]))
    if status != 0:
        raise RuntimeError('failed to store S3 credentials: %s' % stderr.read())
    status, _, stderr = run_command(
        client, "s3fs %s %s -o allow_other -o passwd_file=s3_passwd" % (
            corpus["bucket"], corpus["mount"]))
    if status != 0:
        raise RuntimeError('failed to fuse S3 bucket: %s' % stderr.read())

def check_environment(client, lgpu, log_dir, docker_registries, requirements, check=True):
    """Check that the environment contains all the tools necessary to launch a task
       and meets requirement
    """
    for registry in six.itervalues(docker_registries):
        if registry['type'] == 'aws' and not program_exists(client, 'aws'):
            raise EnvironmentError("missing aws client")

    # check log_dir
    if not run_and_check_command(client, "test -d '%s'" % log_dir):
        raise EnvironmentError("missing log directory: %s" % log_dir)

    if not program_exists(client, "docker"):
        raise EnvironmentError("docker not available")
    if len(lgpu) == 0:
        return {}
    else:
        if not program_exists(client, "nvidia-docker"):
            raise EnvironmentError("nvidia-docker not available")

        usage = { 'gpus':[], 'disk':[] }
        for gpu_id in lgpu:
            gpu_id = int(gpu_id)
            exit_status, stdout, stderr = run_command(
                client, 'nvidia-smi -q -i %d -d UTILIZATION,MEMORY' % (gpu_id - 1))
            if exit_status != 0:
                raise EnvironmentError("gpu check failed (nvidia-smi error %d: %s)" % (
                    exit_status, stderr.read()))

            out = stdout.read()
            gpu = '?'
            mem = '?'
            m = re.search(b'Gpu *: (.*) *\n', out)
            if m:
                gpu = m.group(1).decode('utf-8')
            m = re.search(b'Free *: (.*) MiB *\n', out)
            if m:
                mem = int(m.group(1).decode('utf-8'))
            usage['gpus'].append({'gpuid': gpu_id, 'usage': gpu, 'mem':mem})
            if check and requirements:
                if "free_gpu_memory" in requirements and mem < requirements["free_gpu_memory"]:
                    raise EnvironmentError("not enough gpu memory available on gpu %d: %d/%d"
                                           % (gpu_id, mem, requirements["free_gpu_memory"]))

        if requirements and "free_disk_space" in requirements:    
            for path, space_G in six.iteritems(requirements["free_disk_space"]):
                exit_status, stdout, stderr = run_command(
                    client,
                    "set -o pipefail; df --output=avail -BG %s | tail -1 | awk '{print $1}'" % path)

                if exit_status != 0:
                    raise EnvironmentError("missing directory %s" % (path))
                out = stdout.read().strip()
                m = re.search(b'([0-9]+)G', out)
                if check and (m is None or int(m.group(1).decode('utf-8')) < space_G):
                    raise EnvironmentError("not enough free diskspace on %s: %s/%dG"
                                   % (path, out, space_G))
                usage['disk'].append({'path':path, 'free': out, 'required': space_G})

        return usage

def cmd_connect_private_registry(docker_registry):
    if docker_registry['type'] == "aws":
        return ('$(AWS_ACCESS_KEY_ID=%s AWS_SECRET_ACCESS_KEY=%s '
                'aws ecr get-login --no-include-email --region %s)') % (
                    docker_registry['credentials']['AWS_ACCESS_KEY_ID'],
                    docker_registry['credentials']['AWS_SECRET_ACCESS_KEY'],
                    docker_registry['region'])
    username = docker_registry['credentials']['username']
    password = docker_registry['credentials']['password']
    return ('docker login --username %s --password %s') % (username, password)

def cmd_docker_pull(image_ref, docker_path=None):
    path = ""
    if docker_path is not None:
        path = docker_path + "/"
    return '%sdocker pull %s' % (path, image_ref)

def cmd_docker_run(gpu_id, docker_options, task_id,
                   docker_image, image_ref, callback_url, callback_interval,
                   storages, docker_command, log_dir=None, sep=" "):
    env = {}
    nbgpu = len(gpu_id)
    nv_gpu = ''
    if nbgpu == 1 and gpu_id[0] == 0:
        gpu_id = '0'
    else:
        env['NV_GPU'] = str(",".join([str(int(g)-1) for g in gpu_id]))
        gpu_id = ",".join([str(v) for v in range(1, nbgpu+1)])

    if docker_options.get('dev') == 1:
        return "sleep%s35" % sep
    else:
        docker_cmd = 'docker' if gpu_id == 0 else 'nvidia-docker'
        docker_path = docker_options.get('path')
        if docker_path:
            docker_cmd = docker_path +'/' + docker_cmd

        # launch the task
        cmd = '%s_o_run_o_-i_o_--rm' % docker_cmd
        if 'mount' in docker_options:
            for k in docker_options['mount']:
                cmd += '_o_-v_o_%s' % k
        if 'envvar' in docker_options:
            for k, v in six.iteritems(docker_options['envvar']):
                if isinstance(v, str):
                    cmd += '_o_-e_o_%s=%s' % (k, v)
                elif isinstance(v, dict) and k == "specific" and docker_image in v:
                    # specific options for a given image
                    for ks, vs in six.iteritems(v[docker_image]):
                        cmd += '_o_-e_o_%s=%s' % (ks, vs)

        # mount TMP_DIR used to store potential transfered files
        cmd += '_o_-e_o_TMP_DIR=/root/tmp/%s' % task_id

        cmd += '_o_%s' % image_ref

        if storages is not None and storages != {}:
            v = json.dumps(storages)
            v = v.replace("<TASK_ID>", task_id)
            v = v.replace("<CALLBACK_URL>", callback_url)
            cmd += '_o_-s_o_%s' % v

            # if model storage is not specified, check if there is a default
            # model storage
            if '-ms' not in docker_command:
                for s in storages:
                    if storages[s].get('default_ms'):
                        docker_command = ['-ms', s + ':'] + docker_command
                        break

        cmd += '_o_-g_o_%s' % gpu_id
        cmd += '_o_-t_o_%s' % task_id
        if callback_url is not None and callback_url != '':
            cmd += '_o_-b_o_%s' % callback_url
            if callback_interval is not None:
                cmd += '_o_-bi_o_%d' % callback_interval

        cmd += '_o_-i_o_%s' % image_ref

        for arg in docker_command:
            if arg.startswith('${TMP_DIR}'):
                arg = '/root/tmp/%s%s' % (task_id, arg[10:])
            cmd += '_o_' + arg

        return cmd.replace("\n","\\\\n").replace("_o_","\n"), str(env).replace("'",'"')

def update_log(task_id,
               client,
               log_dir,
               callback_url):
    log_file = "%s/%s.log" % (log_dir, task_id)
    cmd = 'curl -X POST "%s/task/log/%s" --data-binary "@%s"' % (
                callback_url, task_id, log_file)
    _, stdout, stderr = run_command(client, cmd)

def launch_task(task_id,
                client,
                lgpu,
                log_dir,
                docker_options,
                the_docker_registry,
                docker_image,
                docker_tag,
                docker_command,
                docker_files,
                wait_for_immediate_failure=2,
                storages=None,
                callback_url=None,
                callback_interval=None,
                requirements=None):
    """Launch a task:
        * `task_id`: assigned id for the task and used for logging
        * `client`: ssh client
        * `gpu_id`: if > 0, the id (eventually ids) of the GPU to use on the host
        * `log_dir`: host docker log
        * `docker_options`: environment and mounting points
        * `docker_image`: image name of the docker
        * `docker_tag`: tag - by default latest
        * `docker_command`: the actual command to launch
        * `wait_for_immediate_failure`: time to wait and monitor launched process
        * `storages`: dictionary of storage to use in the docker execution
        * `callback_url`: server to callback for beat of activity
        * `callback_interval`: time between 2 beats
    """
    gpu_id = ",".join([gpu_id for gpu_id in lgpu])
    logger.info("launching task - %s / %s", task_id, gpu_id)
    logger.debug("check environment for task %s", task_id)
    check_environment(client, lgpu, log_dir,
                      {the_docker_registry:
                              docker_options['registries'][the_docker_registry]},
                       requirements)

    image_ref = ""
    if docker_options.get('dev') != 1:
        docker_registry = docker_options['registries'][the_docker_registry]

        registry_uri = docker_registry['uri']

        # connect to a registry
        if docker_registry['type'] != 'dockerhub':
            exit_status, stdout, stderr = run_command(
                client,
                cmd_connect_private_registry(docker_registry)
            )
            if exit_status != 0:
                raise EnvironmentError("cannot connect to private registry: %s" % stderr.read())

        # pull the docker image
        registry_urip = '' if registry_uri == '' else registry_uri + '/'
        image_ref = '%s%s:%s' % (registry_urip, docker_image, docker_tag)
        logger.debug("pulling docker image: %s - %s", docker_registry['type'], docker_image)
        docker_cmd = cmd_docker_pull(image_ref, docker_path=docker_options.get('path'))
        exit_status, stdout, stderr = run_command(client, docker_cmd)
        if exit_status != 0:
            raise RuntimeError("error pulling the image %s: %s" % (image_ref, stderr.read()))

    if len(docker_files):
        # we have files to synchronize locally
        assert 'mount' in docker_options, "mount point should be defined for passing files"
        assert callback_url is not None, "callback_url needed for passing files"
        mount_tmpdir = None
        for m in docker_options['mount']:
            if m.endswith('/root/tmp'):
                mount_tmpdir = m[:-10]
                break
        assert mount_tmpdir is not None, "mount points need to include /root/tmp for passing files"
        cmd_mkdir = "mkdir -p %s/%s" % (mount_tmpdir, task_id)
        exit_status, stdout, stderr = run_command(client, cmd_mkdir)
        if exit_status != 0:
            raise RuntimeError("error build task tmp dir: %s, %s" % (cmd_mkdir, stderr.read()))
        for f in docker_files:
            p = f.rfind("/")
            if p != -1:
                fdir = f[:p]
                cmd_mkdir = "mkdir -p %s/%s/%s" % (mount_tmpdir, task_id, fdir)
                exit_status, stdout, stderr = run_command(client, cmd_mkdir)
                if exit_status != 0:
                    s = stderr.read()
                    raise RuntimeError("error build task tmp sub-dir: %s, %s" % (cmd_mkdir, stderr.read()))
            logger.info("retrieve file %s -> %s/%s", f, mount_tmpdir, task_id)
            cmd_get_files = 'curl "%s/task/file/%s/%s" > %s/%s/%s' % (
                callback_url,
                task_id,
                f,
                mount_tmpdir,
                task_id,
                f)
            exit_status, stdout, stderr = run_command(client, cmd_get_files)
            if exit_status != 0:
                raise RuntimeError("error retrieving files: %s, %s" % (cmd_get_files, stderr.read()))

    if callback_url is not None:
        exit_status, stdout, stderr = run_command(client, "curl -m 5 -s -X POST '%s/task/log/%s'"
                                                          " --data-ascii 'Initialization complete...'" %
                                                    (callback_url, task_id))
        if exit_status != 0:
            raise RuntimeError("cannot send beat back (%s) - aborting" % stderr.read())

    cmd, env = cmd_docker_run(lgpu, docker_options, task_id,
                              docker_image, image_ref, callback_url, callback_interval,
                              storages, docker_command, log_dir)
    cmd = "nohup python -c \'" + python_run % (task_id, cmd, "%s/%s.log" % (log_dir, task_id), callback_url or '', env) + "'"

    # get the process group id
    cmd += ' & ps -o pgid -p $!'

    exit_status, stdout, stderr = run_command(client, cmd, handlePrivate=False)
    if exit_status != 0:
        raise RuntimeError("%s run failed: %s" % (cmd, stderr.read()))

    # read ps header
    outpgid = stdout.readline()
    # read pgid
    outpgid = stdout.readline()
    m = re.search(r'(\d+)', outpgid)
    if not m:
        raise RuntimeError("cannot get PGID")
    pgid = int(m.group(1))
    logger.info("Process launched with pgid %d.", pgid)

    # check what is happening 1s later - just to check immediate failure
    if wait_for_immediate_failure > 0:
        logger.info("Wait for %d seconds and check process status.", wait_for_immediate_failure)
        time.sleep(wait_for_immediate_failure)
        if not run_and_check_command(client, 'kill -0 -%d' % pgid):
            log_file = "%s/%s.log" % (log_dir, task_id)
            _, stdout, stderr = run_command(client, 'cat %s' % log_file)
            raise RuntimeError("process exited early: %s" % stdout.read())

    return {"model": task_id, "pgid": pgid}
