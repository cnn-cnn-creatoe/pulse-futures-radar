"""OS-owned process lock, released automatically after a crash."""
import os
from pathlib import Path


class InstanceLock:
    def __init__(self,path):
        self.path=Path(path)
        self.handle=None

    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        handle=self.path.open('a+b')
        try:
            if handle.tell()==0:
                handle.write(b'0');handle.flush()
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise RuntimeError('本项目后台已在运行；为避免同一 IP 重复采集，已阻止第二个实例') from error
        self.handle=handle

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle=None
