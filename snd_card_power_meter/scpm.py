#! /usr/bin/python

""" 
THIS CODE IS JUST ME LEARNING TO USE ALSAAUDIO.
IT DOESN'T YET DO ANY POWER MEASUREMENT STUFF! 

Requirements
------------

  Ubuntu packages
     sudo apt-get install python-dev python-pip alsa alsa-tools libasound2-dev

  pyalsaaudio
     * Install on Linux using "sudo pip install pyalsaaudio"
                           or "sudo easy_install pyalsaaudio".

  On Linux, add yourself to the audio users:
     sudo adduser USERNAME audio
     (then log out and log back in for this change to take effect)
  
Calibration with a Watts Up
---------------------------

Log data from Watts Up using
   screen -dmL ./wattsup ttyUSB0 volts
  
"""

# TODO:
#  * set mixer levels
#  * do power calcs
#  * sample at 96000, 20-bit
#  * see if wave.writeframes() does proper down-sampling

from __future__ import print_function, division
import numpy as np
import pyaudio # docs: http://people.csail.mit.edu/hubert/pyaudio/
import wave
import matplotlib.pyplot as plt
import subprocess
import sys
import argparse
import audioop # docs: http://docs.python.org/2/library/audioop.html
import time
import wattsup
import collections
from threading import Thread

try:
    from Queue import Queue, Empty
except ImportError:
    from queue import Queue, Empty # python 3.x


CHUNK = 1024
FORMAT = pyaudio.paInt32
CHANNELS = 2
RATE = 96000
RECORD_SECONDS = 1
WAVE_OUTPUT_FILENAME = "output.wav"
# VOLTS_PER_ADC_STEP = 1.07731983340487E-06
VOLTS_PER_ADC_STEP = 1.08181933163E-04

p = pyaudio.PyAudio()
audio_stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                      input=True, frames_per_buffer=CHUNK)


# Named tuple for storing volts and amps
VA = collections.namedtuple('VA', ['time', 'volts', 'amps'])

def run_command(cmd):
    """Run a UNIX shell command.
    
    Args:
        cmd (list of strings)
    """
    try:
        p = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        p.wait()
    except Exception, e:
        print("ERROR: Failed to run '{}'".format(" ".join(cmd)), file=sys.stderr)
        print("ERROR:", str(e), file=sys.stderr)
    else:
        if p.returncode == 0:
            print("Successfully ran '{}'".format(" ".join(cmd)))
        else:
            print("ERROR: Failed to run '{}'".format(" ".join(cmd)), file=sys.stderr)
            print(p.stderr.read(), file=sys.stderr)


def config_mixer():
    print("Configuring mixer...")
    run_command(["amixer", "sset", "Input Source", "Rear Mic"])
    run_command(["amixer", "set", "Digital", "60", "capture"])
    run_command(["amixer", "set", "Capture", "16", "capture"])
    

def get_adc_data():
    """
    Get data from the sound card's analogue to digital converter (ADC).
    
    Returns:
        A named tuple with fields:
        - t (float): UNIX timestamp immediately prior to sampling
        - volts (binary string): raw ADC data 
        - amps (binary string): see 'volts'
    """
    t = time.time()
    frames = []
    for i in range(0, int(RATE / CHUNK * RECORD_SECONDS)):
        try:
            data = audio_stream.read(CHUNK)
        except IOError, e:
            print("ERROR: ", str(e), file=sys.stderr)
            # TODO: should we do i-=1 ?
        else:
            frames.append(data)

    stereo = b''.join(frames)
    width = p.get_sample_size(FORMAT)
    volts = audioop.tomono(stereo, width, 0, 1)
    amps = audioop.tomono(stereo, width, 1, 0)
    return VA(t, volts, amps)


def enqueue_adc_data(adc_data_queue):
    """This will be run as a separate thread""" 
    while True:
        adc_data_queue.put(get_adc_data())


def record_data(adc_data_queue):
    adc_data = adc_data_queue.get()
    np_data = np.fromstring(adc_data.volts, dtype='int32')
    print(audioop.rms(adc_data.volts, p.get_sample_size(FORMAT)))
    sys.stdout.flush()
    print("mean = {:4.1f}v, rms = {:4.1f}v".format(
                                np_data.mean() * VOLTS_PER_ADC_STEP,
                                audioop.rms(adc_data.volts, p.get_sample_size(FORMAT)) * VOLTS_PER_ADC_STEP ))
    # plt.plot(np_data)
    # plt.show()
    
def find_time(adc_data_queue, target_time):
    """
    Looks through adc_data_queue for an entry with time=t.
    If an entry is found then that entry is returned and that entry and all
    previous entries are deleted from the Queue.
    If no matching entry is found the None is returned.
    
    Beware: If the queue contains at least one entry then EVERY call to this
        function will take at least one item off the queue, even if that
        item is not returned! 
    
    Args:
        adc_data_queue (Queue)
        target_time (int): UNIX timestamp
    """
    t = 0
    while t < target_time:
        adc_data = None                    
        try:
            adc_data = adc_data_queue.get_nowait()
        except Empty:
            return None
        else:
            t = int(round(adc_data.time))
    
    if t == target_time:
        return adc_data
    else:
        return None

def calibrate(adc_data_queue):
    wu = wattsup.WattsUp()
    
    v_acumulator = 0.0 # accumulator for voltage
    n_v_samples = 0 # number of voltage samples
    
    while True:
        adc_data = adc_data_queue.get() # blocks if queue is empty
        adc_v_rms = audioop.rms(adc_data.volts, p.get_sample_size(FORMAT))
        
        wu_data = wu.get() # blocks
        if wu_data:
            n_v_samples += 1
            v_acumulator += wu_data.volts / adc_v_rms
            av_v_calibration = v_acumulator / n_v_samples # average voltage calibration
            print("WattsUp Volts = {}, WattsUp amps = {}, v_calibration = {}\n"
                  ", adc_data.time = {}, wu_data.time = {}, diff = {}"
                  .format(wu_data.volts, wu_data.amps, av_v_calibration,
                          adc_data.time, wu_data.time, adc_data.time - wu_data.time))
            print("volts = {}".format( adc_v_rms * av_v_calibration))
            
        # TODO:
        #   check that adc_data.time and wu_data.time are similar.  What happen
        #      if wu.get_last_nowait() falls over for a while and gives us nothing?
        #      then we'll be reading old data from adc_data = queue.get()
        #   write this to disk
        #   do calcs for amps


def setup_argparser():
    # Process command line _args
    parser = argparse.ArgumentParser(description="Record voltage and current"
                                     "  waveforms using the sound card.")
       
    parser.add_argument("--no-mixer-config", dest="config_mixer", 
                        action="store_false",
                        help="Don't modify ALSA mixer (i.e. use existing system settings)")
    
    parser.add_argument("--calibrate", 
                        action="store_true",
                        help="Run calibration using a WattsUp meter")
    
    args = parser.parse_args()

    return args

def start_adc_data_queue_and_thread():
    adc_data_queue = Queue()
    adc_thread = Thread(target=enqueue_adc_data, args=(adc_data_queue, ))
    adc_thread.daemon = True # this thread dies with the program
    adc_thread.start()
    
    return adc_data_queue, adc_thread    
    

def main():
    args = setup_argparser()
    
    if args.config_mixer:
        config_mixer()

    adc_data_queue, adc_thread = start_adc_data_queue_and_thread()

    if args.calibrate:
        calibrate(adc_data_queue)
    else:
        while True:
            try:
                record_data(adc_data_queue)
            except KeyboardInterrupt:
                audio_stream.stop_stream()
                audio_stream.close()
                p.terminate()
                break
        
    print("")

if __name__=="__main__":
    main()
