# simulation object contains the current state of the simulation.
# It is analagous to the "bundle" object in the original FMS code.
import types
import math
import numpy as np
import h5py
from pyspawn.fmsobj import fmsobj
from pyspawn.traj import traj
import general as gen
import os
import shutil
import complexgaussian as cg
import datetime
import time

class simulation(fmsobj):
    
    def __init__(self):
        # traj is a dictionary of trajectory basis functions (TBFs)
        self.traj = dict()

        # queue is a list of tasks to be run
        self.queue = ["END"]
        # tasktimes is a list of the simulation times associated with each task
        self.tasktimes = [1e10]

        # olapmax is the maximum overlap allowed for a spawn.  Above this,
        # the spawn is cancelled
        self.olapmax = 0.8

        # quantum time is the current time of the quantum amplitudes
        self.quantum_time = 0.0
        # quantum time is the current time of the quantum amplitudes
        self.quantum_time_half_step = 0.0
        # timestep for quantum propagation
        self.timestep = 0.0
        # quantum propagator
        #self.qm_propagator = "RK2"
        # quantum hamiltonian
        #self.qm_hamiltonian = "adiabatic"

        # maps trajectories to matrix element indices
        self.traj_map = dict()

        # quantum amplitudes
        self.qm_amplitudes = np.zeros(0,dtype=np.complex128)

        # energy shift for quantum propagation
        self.qm_energy_shift = 0.0

        # variables to be output to hdf5 mapped to the size of each data point
        self.h5_datasets = dict()
        self.h5_types = dict()

        # maximium walltime in seconds
        self.max_quantum_time = -1.0

        # maximium walltime in seconds
        self.max_walltime = -1.0

    def from_dict(self,**tempdict):
        """convert dict to simulation data structure"""
        for key in tempdict:
            if isinstance(tempdict[key],types.UnicodeType) :
                tempdict[key] = str(tempdict[key])
            if isinstance(tempdict[key],types.ListType) :
                if isinstance((tempdict[key])[0],types.FloatType) :
                    # convert 1d float lists to np arrays
                    tempdict[key] = np.asarray(tempdict[key])
                if isinstance((tempdict[key])[0],types.StringTypes) :
                    if (tempdict[key])[0][0] == "^":
                        for i in range(len(tempdict[key])):
                            tempdict[key][i] = eval(tempdict[key][i][1:])
                        tempdict[key] = np.asarray(tempdict[key],dtype=np.complex128)
                else:
                    if isinstance((tempdict[key])[0],types.ListType):
                        if isinstance((tempdict[key])[0][0],types.FloatType) :
                            # convert 2d float lists to np arrays
                           tempdict[key] = np.asarray(tempdict[key])
                        if isinstance((tempdict[key])[0][0],types.StringTypes) :
                            if (tempdict[key])[0][0][0] == "^":
                                for i in range(len(tempdict[key])):
                                    for j in range(len(tempdict[key][i])):
                                        tempdict[key][i][j] = eval(tempdict[key][i][j][1:])
                                tempdict[key] = np.asarray(tempdict[key],dtype=np.complex128)
            if isinstance(tempdict[key],types.DictType) :
                if 'fmsobjlabel' in (tempdict[key]).keys():
                    fmsobjlabel = (tempdict[key]).pop('fmsobjlabel')
                    obj = eval(fmsobjlabel[8:])()
                    obj.from_dict(**(tempdict[key]))
                    tempdict[key] = obj
                else:
                    for key2 in tempdict[key]:
                        if isinstance((tempdict[key])[key2],types.DictType) :
                            fmsobjlabel = ((tempdict[key])[key2]).pop('fmsobjlabel')
                            obj = eval(fmsobjlabel[8:])()
                            obj.from_dict(**((tempdict[key])[key2]))
                            (tempdict[key])[key2] = obj
        self.__dict__.update(tempdict)

    def add_traj(self, t1):
        """add a trajectory to the simulation"""
        key = t1.label
        print "Trajectory added:", key
        mintime = t1.mintime
        index = -1
        for key2 in self.traj:
            if mintime < self.traj[key2].mintime:
                if index < 0:
                    index = self.traj_map[key2]
                self.traj_map[key2] += 1
        if index < 0:
            index = len(self.traj)
        self.traj[key] = t1
        self.traj_map[key] = index
        #sort traj_map by mintime

    def get_numtasks(self):
        """get the number of tasks in the queue"""
        return (len(self.queue)-1)

    def propagate(self):
        """this is the main propagation loop for the simulation"""
        gen.print_splash()
        while True:

            # update the queue (list of tasks to be computed)
            print "### updating task queue"
            self.update_queue()

            # if the queue is empty, we're done!
            print "### checking if we are at the end of the simulation"
            #if (self.queue[0] == "END"):
            if (self.quantum_time + 1.0e-6 > self.max_quantum_time):
                print "### propagate DONE, simulation ended gracefully!"
                return

            # end simulation if walltime has expired
            print "### checking if maximum wall time is reached"
            if (self.max_walltime < time.time() and self.max_walltime > 0):
                print "### wall time expired, simulation ended gracefully!"
                return
            
            # it is possible for the queue to run empty but for the job not to be done
            if (self.queue[0] != "END"):            
                # Right now we just run a single task per cycle,
                # but we could parallelize here and send multiple tasks
                # out for simultaneous processing.
                current = self.pop_task()
                print "### starting " + current            
                eval(current)
                print "### done with " + current
            else:
                print "### task queue is empty"
            print "### now we will clone new trajectories if necessary"
            self.clone_as_necessary()
            
            # propagate quantum variables if possible
            print "### propagating quantum amplitudes if we have enough information to do so"
            self.propagate_quantum_as_necessary()
            
            # print restart output - this must be the last line in this loop!
            print "### updating restart output"
            self.restart_output()    
        
    def propagate_quantum_as_necessary(self):
        """here we will propagate the quantum amplitudes if we have
        the necessary information to do so.
        we have to determine what the maximum time is for which
        we have all the necessary information to propagate the amplitudes"""
        max_info_time = 1.0e10
        # first check trajectories
        for key in self.traj:

            timestep = self.traj[key].timestep
            time = self.traj[key].time
            if (time - timestep) < max_info_time:
                max_info_time = time - timestep

        print "## we have enough information to propagate to time ", max_info_time

        # now, if we have the necessary info, we propagate
        while max_info_time > (self.quantum_time + 1.0e-6):
            if self.quantum_time > 1.0e-6:
                print "## propagating quantum amplitudes at time", self.quantum_time
                self.qm_propagate_step()
            else:
                print "## propagating quantum amplitudes at time", self.quantum_time, " (first step)"
                self.qm_propagate_step(zoutput_first_step=True)
                
            print "## outputing quantum information to hdf5"
            self.h5_output()

    def init_amplitudes_one(self):
        """sets the first amplitude to 1.0 and all others to zero"""
        self.compute_num_traj_qm()
        self.qm_amplitudes = np.zeros_like(self.qm_amplitudes, dtype=np.complex128)
        self.qm_amplitudes[0] = 1.0
        
    def compute_num_traj_qm(self):
        n = 0
        qm_time = self.quantum_time
        for key in self.traj:
            if qm_time > (self.traj[key].mintime - 1.0e-6):
                n += 1
        self.num_traj_qm = n
        while n > len(self.qm_amplitudes):
            self.qm_amplitudes = np.append(self.qm_amplitudes,0.0)
                
    def get_qm_data_from_h5(self):
        """get the necessary geometries and energies from hdf5"""
        qm_time = self.quantum_time
        ntraj = self.num_traj_qm
        for key in self.traj:
            if self.traj_map[key] < ntraj:
                self.traj[key].get_all_qm_data_at_time_from_h5(qm_time)
            
    def get_qm_data_from_h5_half_step(self):
        qm_time = self.quantum_time_half_step
        ntraj = self.num_traj_qm
        for key in self.traj:
            if self.traj_map[key] < ntraj:
                self.traj[key].get_all_qm_data_at_time_from_h5_half_step(qm_time)
                        
    def build_S(self):
        """Building overlap matrix"""
        ntraj = self.num_traj_qm
        self.S = np.zeros((ntraj,ntraj), dtype=np.complex128)
        for keyi in self.traj:
            i = self.traj_map[keyi]
            if i < ntraj:
                for keyj in self.traj:
                    j = self.traj_map[keyj]
                    if j < ntraj:
                        self.S[i,j] = cg.overlap_nuc_elec(self.traj[keyi], self.traj[keyj],\
                                                          positions_i="positions_qm",\
                                                          positions_j="positions_qm",\
                                                          momenta_i="momenta_qm",\
                                                          momenta_j="momenta_qm")
    
    def build_Sdot(self):
        """build the right-acting time derivative operator"""
        ntraj = self.num_traj_qm
        self.Sdot = np.zeros((ntraj,ntraj), dtype=np.complex128)
        for keyi in self.traj:
            i = self.traj_map[keyi]
            if i < ntraj:
                for keyj in self.traj:
                    j = self.traj_map[keyj]
                    if j < ntraj:
                        self.Sdot[i,j] = cg.Sdot_nuc_elec(self.traj[keyi],\
                                                          self.traj[keyj],\
                                                          positions_i="positions_qm",\
                                                          positions_j="positions_qm",\
                                                          momenta_i="momenta_qm",\
                                                          momenta_j="momenta_qm",\
                                                          forces_j="forces_i_qm")

    def invert_S(self):
        """compute Sinv from S"""
        self.Sinv = np.linalg.inv(self.S)
        
    def build_H(self):
        """building the Hamiltonian matrix, H
        this routine assumes that S is already built"""
        
        print "# building potential energy matrix"
        self.build_V()
        print "# building kinetic energy matrix"
        self.build_T()
        ntraj = self.num_traj_qm
        shift = self.qm_energy_shift * np.identity(ntraj)
        print "# summing Hamiltonian"
        self.H = self.T + self.V + shift

    def build_V(self):
        """build the potential energy matrix, V
        This routine assumes that S is already built"""
        c1i = (complex(0.0,1.0))
        cm1i = (complex(0.0,-1.0))
        ntraj = self.num_traj_qm
        self.V = np.zeros((ntraj,ntraj),dtype=np.complex128)
        for key in self.traj:
            i = self.traj_map[key]
            istate = self.traj[key].istate
            if i < ntraj:
                self.V[i,i] = self.traj[key].energies_qm[istate]
                
    def build_T(self):
        """building the kinetic energy matrix, T"""
        ntraj = self.num_traj_qm
        self.T = np.zeros((ntraj,ntraj), dtype=np.complex128)
        for keyi in self.traj:
            i = self.traj_map[keyi]
            if i < ntraj:
                for keyj in self.traj:
                    j = self.traj_map[keyj]
                    if j < ntraj:
                        self.T[i,j] = cg.kinetic_nuc_elec(self.traj[keyi],\
                                                          self.traj[keyj],\
                                                          positions_i="positions_qm",\
                                                          positions_j="positions_qm",\
                                                          momenta_i="momenta_qm",\
                                                          momenta_j="momenta_qm")
  
    def build_Heff(self):
        """built Heff form H, Sinv, and Sdot"""
        print "# building effective Hamiltonian"
        c1i = (complex(0.0,1.0))
        self.Heff = np.matmul(self.Sinv, (self.H - c1i * self.Sdot))

    def pop_task(self):
        """pop the task from the top of the queue"""
        return self.queue.pop(0)

    def update_queue(self):
        """build a list of all tasks that need to be completed"""
        while self.queue[0] != "END":
            self.queue.pop(0)
        tasktimes=[1e10]
        
        # forward propagation tasks
        for key in self.traj:
            if (self.traj[key].maxtime + 1.0e-6) > self.traj[key].time:
                task_tmp = "self.traj[\"" + key  + "\"].propagate_step()"
                tasktime_tmp = self.traj[key].time
                self.insert_task(task_tmp,tasktime_tmp, tasktimes)
                
        print "##", (len(self.queue)-1), "task(s) in queue:"
        for i in range(len(self.queue)-1):
            print self.queue[i] + ", time = " + str(tasktimes[i])
        print "END"

    def insert_task(self,task,tt,tasktimes):
        """add a task to the queue"""
        for i in range(len(tasktimes)):
            if tt < tasktimes[i]:
                self.queue.insert(i,task)
                tasktimes.insert(i,tt)
                return

    def clone_as_necessary(self):
        """cloning routine"""
        clonetraj = dict()
        for key in self.traj:
            # trajectories that are cloning or should start were marked
            # during propagation.  See "propagate_step" and "consider_spawning"
            # in traj.py
            z = self.traj[key].z_clone_now
            for jstate in range(self.traj[key].numstates):
                # is this trajectory marked to spawn to state j?
                if z[jstate] > 0.5:
                    # create label that indicates parentage
                    # for example: a trajectory labeled 00b1b5 means that the initial
                    # trajectory "00" spawned a trajectory "1" (its
                    # second child) which then spawned another (it's 6th child)
                    label = str(self.traj[key].label) + "b" + str(self.traj[key].numchildren)

                    # create and initiate new trajectory structure
                    newtraj = traj()
                    newtraj.init_clone_traj(self.traj[key], jstate, label)

                    # checking to see if overlap with existing trajectories
                    # is too high.  If so, we abort spawn
#                     z_add_traj_olap = self.check_overlap(newtraj)

                    # rescaling velocity.  We'll abort if there is not
                    # enough energy (aka a "frustrated spawn")
                    z_add_traj_rescale = newtraj.rescale_momentum(self.traj[key].av_energy)
#                     rescale_parent = sel#f.traj[key].rescale_parent_momentum(newtraj.av_energy *\
#                                                                             (1 - newtraj.populations[jstate]))
                    # okay, now we finally decide whether to spawn or not
                    if z_add_traj_rescale: #and rescale_parent:
                        print "## creating new trajectory ", label
                        clonetraj[label] = newtraj
                        self.traj[key].numchildren += 1
                        # After cloning to jstate we should remove population on jth state from the parent
                        self.traj[key].remove_state_pop(jstate)
                         
                    # whether we spawn or not, we reset the trajectory so
                    # that:
                    # it isn't slated to spawn
                    z[jstate] = 0.0
                    # it isn't currently spawning

            # once all states have been checked, we update the TBF structure
            self.traj[key].z_clone_now = z

        # okay, now it's time to add the spawned trajectories
        for label in clonetraj:
            
            # finally, add the spawned trajectory
            print "CLONING SUCCESSFULL:"
            print "number of trajectories:", self.num_traj_qm
            self.add_traj(clonetraj[label])
            print "number of trajectories:", self.num_traj_qm
            
    def check_overlap(self, newtraj):
        """check to make sure that a cloned trajectory doesn't overlap too much
        with any existing trajectory (IS THIS NEEDED?)"""
        z_add_traj = True
        for key2 in self.traj:
            # compute the overlap
            overlap = cg.overlap_nuc_elec(newtraj, self.traj[key2],\
                                          positions_j="positions_tmdt",\
                                          momenta_j="momenta_tmdt")

            # if the overlap is too high, don't spawn!
            if np.absolute(overlap) > self.olapmax:
                z_add_traj=False

            # let the user know what happened
            if not z_add_traj:
                print "# aborting spawn due to large overlap with existing trajectory"
        return z_add_traj

    def restart_from_file(self, json_file, h5_file):
        """restarts from the current json file and copies the simulation data into working.hdf5"""
        self.read_from_file(json_file)
        shutil.copy2(h5_file, "working.hdf5")
        
    def restart_output(self):
        """output json restart file
        The json file is meant to represent the *current* state of the
        simulation.  There is a separate hdf5 file that stores the history of
        the simulation.  Both are needed for restart."""
        print "## creating new sim.json" 
        # we keep copies of the last 3 json files just to be safe
        extensions = [3,2,1,0]
        for i in extensions :
            if i==0:
                ext = ""
            else:
                ext = str(i) + "."
            filename = "sim." + ext + "json"
            if os.path.isfile(filename):
                if (i == extensions[0]):
                    os.remove(filename)
                else:
                    ext = str(i+1) + "."
                    filename2 = "sim." + ext + "json"
                    if (i == extensions[-1]):
                        shutil.copy2(filename, filename2)
                    else:
                        shutil.move(filename, filename2)
                        
        # now we write the current json file
        self.write_to_file("sim.json")
        print "## synchronizing sim.hdf5"
        extensions = [3,2,1,0]
        for i in extensions :
            if i==0:
                ext = ""
            else:
                ext = str(i) + "."
            filename = "sim." + ext + "hdf5"
            if os.path.isfile(filename):
                if (i == extensions[0]):
                    os.remove(filename)
                else:
                    ext = str(i+1) + "."
                    filename2 = "sim." + ext + "hdf5"
                    if (i == extensions[-1]):
                        shutil.copy2(filename, filename2)
                    else:
                        shutil.move(filename, filename2)
        shutil.copy2("working.hdf5", "sim.hdf5")
        print "## hdf5 and json output are synchronized"
        
    def h5_output(self):
        self.init_h5_datasets()
        filename = "working.hdf5"
        h5f = h5py.File(filename, "a")
        groupname = "sim"
        if groupname not in h5f.keys():
            # creating sim group in hdf5 output file
            self.create_h5_sim(h5f, groupname)
            grp = h5f.get(groupname)
            self.create_new_h5_map(grp)
        else:
            grp = h5f.get(groupname)
        znewmap = False
        for key in self.h5_datasets:
            n = self.h5_datasets[key]
            dset = grp.get(key)
            l = dset.len()
            if l > 0:
                lwidth = dset.size / l
                if n > lwidth:
                    dset.resize(n, axis=1)
                    if not znewmap:
                        self.create_new_h5_map(grp)
                        znewmap = True
            dset.resize(l+1, axis=0)
            ipos=l
#             getcom = "self.get_" + key + "()"
            getcom = "self." + key
            #print getcom
            tmp = eval(getcom)
            if type(tmp).__module__ == np.__name__:
                tmp = np.ndarray.flatten(tmp)
                dset[ipos, 0:n] = tmp[0:n]
            else:
                dset[ipos, 0] = tmp
        h5f.flush()
        h5f.close()

    def create_new_h5_map(self, grp):
        ntraj = self.num_traj_qm
        labels = np.empty(ntraj, dtype="S512")
        istates = np.zeros(ntraj, dtype=np.int32)
        for key in self.traj_map:
            if self.traj_map[key] < ntraj:
                labels[self.traj_map[key]] = key
                istates[self.traj_map[key]] = self.traj[key].istate
        grp.attrs["labels"] = labels
        grp.attrs["istates"] = istates
        
    def create_h5_sim(self, h5f, groupname):
        trajgrp = h5f.create_group(groupname)
        for key in self.h5_datasets:
            n = self.h5_datasets[key]
            dset = trajgrp.create_dataset(key, (0,n), maxshape=(None,None), dtype=self.h5_types[key])

    def init_h5_datasets(self):
        ntraj = self.num_traj_qm
        ntraj2 = ntraj * ntraj
        self.h5_datasets = dict()
        self.h5_datasets["quantum_time"] = 1
        self.h5_datasets["qm_amplitudes"] = ntraj
        self.h5_datasets["Heff"] = ntraj2
        self.h5_datasets["H"] = ntraj2
        self.h5_datasets["S"] = ntraj2
        self.h5_datasets["Sdot"] = ntraj2
        self.h5_datasets["Sinv"] = ntraj2
        self.h5_datasets["num_traj_qm"] = 1
        self.h5_types = dict()
        self.h5_types["quantum_time"] = "float64"
        self.h5_types["qm_amplitudes"] = "complex128"
        self.h5_types["Heff"] = "complex128"
        self.h5_types["H"] = "complex128"
        self.h5_types["S"] = "complex128"
        self.h5_types["Sdot"] = "complex128"
        self.h5_types["Sinv"] = "complex128"
        self.h5_types["num_traj_qm"] = "int32"
        