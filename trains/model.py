import abc
import os
import tarfile
import zipfile
from tempfile import mkdtemp, mkstemp

import pyparsing
import six
from .backend_api.services import models
from pathlib2 import Path
from pyhocon import ConfigFactory, HOCONConverter

from .backend_interface.util import validate_dict, get_single_result, mutually_exclusive
from .debugging.log import get_logger
from .storage import StorageHelper
from .utilities.enum import Options
from .backend_interface import Task as _Task
from .backend_interface.model import create_dummy_model, Model as _Model
from .config import running_remotely, get_cache_dir

ARCHIVED_TAG = "archived"


class Framework(Options):
    """
    Optional frameworks for output model
    """
    tensorflow = 'TensorFlow'
    tensorflowjs = 'TensorFlow_js'
    tensorflowlite = 'TensorFlow_Lite'
    pytorch = 'PyTorch'
    caffe = 'Caffe'
    caffe2 = 'Caffe2'
    onnx = 'ONNX'
    keras = 'Keras'
    mknet = 'MXNet'
    cntk = 'CNTK'
    torch = 'Torch'
    darknet = 'Darknet'
    paddlepaddle = 'PaddlePaddle'
    scikitlearn = 'ScikitLearn'
    xgboost = 'XGBoost'

    __file_extensions_mapping = {
        '.pb': (tensorflow, tensorflowjs, onnx, ),
        '.meta': (tensorflow, ),
        '.pbtxt': (tensorflow, onnx, ),
        '.zip': (tensorflow, ),
        '.tgz': (tensorflow, ),
        '.tar.gz': (tensorflow, ),
        'model.json': (tensorflowjs, ),
        '.tflite': (tensorflowlite, ),
        '.pth': (pytorch, ),
        '.caffemodel': (caffe, ),
        '.prototxt': (caffe, ),
        'predict_net.pb': (caffe2, ),
        'predict_net.pbtxt': (caffe2, ),
        '.onnx': (onnx, ),
        '.h5': (keras, ),
        '.hdf5': (keras, ),
        '.keras': (keras, ),
        '.model': (mknet, cntk, xgboost),
        '-symbol.json': (mknet, ),
        '.cntk': (cntk, ),
        '.t7': (torch, ),
        '.cfg': (darknet, ),
        '__model__': (paddlepaddle, ),
        '.pkl': (scikitlearn, keras, xgboost),
    }

    @classmethod
    def _get_file_ext(cls, framework, filename):
        mapping = cls.__file_extensions_mapping
        filename = filename.lower()

        def find_framework_by_ext(framework_selector):
            for ext, frameworks in mapping.items():
                if frameworks and filename.endswith(ext):
                    fw = framework_selector(frameworks)
                    if fw:
                        return (fw, ext)

        # If no framework, try finding first framework matching the extension, otherwise (or if no match) try matching
        # the given extension to the given framework. If no match return an empty extension
        return (
            (not framework and find_framework_by_ext(lambda frameworks_: frameworks_[0]))
            or find_framework_by_ext(lambda frameworks_: framework if framework in frameworks_ else None)
            or (framework, filename.split('.')[-1] if '.' in filename else '')
        )


@six.add_metaclass(abc.ABCMeta)
class BaseModel(object):
    _package_tag = "package"

    @property
    def id(self):
        """
        return the id of the model (string)

        :return: model id (string)
        """
        return self._get_model_data().id

    @property
    def name(self):
        """
        return the name of the model (string)

        :return: model name (string)
        """
        return self._get_model_data().name

    @name.setter
    def name(self, value):
        """
        Update the model name

        :param value: model name (string)
        """
        self._get_base_model().update(name=value)

    @property
    def comment(self):
        """
        return comment/description of the model (string)

        :return: model description (string)
        """
        return self._get_model_data().comment

    @comment.setter
    def comment(self, value):
        """
        Update the model comment/description of the model (string)

        :param value: model comment/description (string)
        """
        self._get_base_model().update(comment=value)

    @property
    def tags(self):
        """
        Return the list of tags the model has

        :return: list of strings (tags)
        """
        return self._get_model_data().tags

    @tags.setter
    def tags(self, value):
        """
        Update the model list of tags (list of strings)

        :param value: list of strings as tags
        """
        self._get_base_model().update(tags=value)

    @property
    def config_text(self):
        """
        returns a string representing the model configuration (from prototxt to ini file or python code to evaluate)

        :return: string
        """
        return _Model._unwrap_design(self._get_model_data().design)

    @property
    def config_dict(self):
        """
        returns a configuration dictionary parsed from the design text,
        usually representing the model configuration (from prototxt to ini file or python code to evaluate)

        :return: Dictionary
        """
        return self._text_to_config_dict(self.config_text)

    @property
    def labels(self):
        """
        Return the labels enumerator {str(label): integer(id)} as saved in the model object

        :return: labels_dict, dictionary with labels (text) keys and values as integers
        """
        return self._get_model_data().labels

    @property
    def task(self):
        return self._task

    @property
    def published(self):
        return self._get_base_model().locked

    @property
    def framework(self):
        return self._get_model_data().framework

    def __init__(self, task=None):
        super(BaseModel, self).__init__()
        self._log = get_logger()
        self._task = None
        self._set_task(task)

    def get_weights(self):
        """
        Download the base model and returns a string of locally stored filename

        :return: string to locally stored file
        """
        # download model (synchronously) and return local file
        return self._get_base_model().download_model_weights()

    def get_weights_package(self, return_path=False):
        """
        Download the base model package, extract the files and return list of locally stored filenames

        :param return_path: if True the model weights are downloaded into a
            temporary directory and the directory path is returned, instead of list of files
        :return: string to locally stored file
        """
        # check if model was packaged
        if self._package_tag not in self._get_model_data().tags:
            raise ValueError('Model is not packaged')

        # download packaged model
        packed_file = self.get_weights()

        # unpack
        target_folder = mkdtemp(prefix='model_package_')
        if not target_folder:
            raise ValueError('cannot create temporary directory for packed weight files')

        for func in (zipfile.ZipFile, tarfile.open):
            try:
                obj = func(packed_file)
                obj.extractall(path=target_folder)
                break
            except (zipfile.BadZipfile, tarfile.ReadError):
                pass
        else:
            raise ValueError('cannot extract files from packaged model at %s', packed_file)

        if return_path:
            return target_folder

        target_files = list(Path(target_folder).glob('*'))
        return target_files

    def publish(self):
        """
        Set the model to 'published' and set it for public use.

        If the model is already published, this method is a no-op.
        """

        if not self.published:
            self._get_base_model().publish()

    def _running_remotely(self):
        return bool(running_remotely() and self._task is not None)

    def _set_task(self, value):
        if value is not None and not isinstance(value, _Task):
            raise ValueError('task argument must be of Task type')
        self._task = value

    @abc.abstractmethod
    def _get_model_data(self):
        pass

    @abc.abstractmethod
    def _get_base_model(self):
        pass

    def _set_package_tag(self):
        if self._package_tag not in self.tags:
            self.tags.append(self._package_tag)
            self._get_base_model().update(tags=self.tags)

    @staticmethod
    def _config_dict_to_text(config):
        if not isinstance(config, dict):
            raise ValueError("Model configuration only supports dictionary objects")
        try:
            # hack, pyhocon is not very good with dict conversion so we pass through json
            try:
                import json
                text = json.dumps(config)
                text = HOCONConverter.convert(ConfigFactory.parse_string(text), 'hocon')
            except Exception:
                # fallback pyhocon
                text = HOCONConverter.convert(ConfigFactory.from_dict(config), 'hocon')
        except Exception:
            raise ValueError("Could not serialize configuration dictionary:\n", config)
        return text

    @staticmethod
    def _text_to_config_dict(text):
        if not isinstance(text, six.string_types):
            raise ValueError("Model configuration parsing only supports string")
        try:
            return ConfigFactory.parse_string(text).as_plain_ordered_dict()
        except pyparsing.ParseBaseException as ex:
            pos = "at char {}, line:{}, col:{}".format(ex.loc, ex.lineno, ex.column)
            six.raise_from(ValueError("Could not parse configuration text ({}):\n{}".format(pos, text)), None)
        except Exception:
            six.raise_from(ValueError("Could not parse configuration text:\n{}".format(text)), None)

    @staticmethod
    def _resolve_config(config_text=None, config_dict=None):
        mutually_exclusive(config_text=config_text, config_dict=config_dict, _require_at_least_one=False)
        if config_dict:
            return InputModel._config_dict_to_text(config_dict)

        return config_text


class InputModel(BaseModel):
    """
    Load an existing model in the system, search by model id.
    The Model will be read-only and can be used to pre initialize a network
    We can connect the model to a task as input model, then when running remotely override it with the UI.
    """

    _EMPTY_MODEL_ID = _Model._EMPTY_MODEL_ID

    @classmethod
    def import_model(
        cls,
        weights_url,
        config_text=None,
        config_dict=None,
        label_enumeration=None,
        name=None,
        tags=None,
        comment=None,
        logger=None,
        is_package=False,
        create_as_published=False,
        framework=None,
    ):
        """
        Create a model from pre-existing model file (link must be valid), and model configuration.

        If the url to the weights file already exists, the import process will stop with a warning
        and automatically it will try to import the model that was found.
        The Model will be read-only and can be used to pre initialize a network
        We can connect the model to a task as input model, then when running remotely override it with the UI.
        Load model based on id, returned object is read-only and can be connected to a task
        That is, we can override the input model when running remotely

        :param weights_url: valid url for the weights file (string).
            examples: "https://domain.com/file.bin" or "s3://bucket/file.bin" or "file:///home/user/file.bin".
            NOTE: if a model with the exact same URL exists, it will be used, and all other arguments will be ignored.
        :param config_text: model configuration  (unconstrained text string). usually the content of
            configuration file. If `config_text` is not None, `config_dict` must not be provided.
        :param config_dict: model configuration parameters (dict).
            If `config_dict` is not None, `config_text` must not be provided.
        :param label_enumeration: dictionary of string to integer, enumerating the model output to labels
            example: {'background': 0 , 'person': 1}
        :param name: optional, name for the newly imported model
        :param tags: optional, list of strings as tags
        :param comment: optional, string description for the model
        :param logger: The logger to use. If None, use the default logger
        :param is_package: Boolean. Indicates that the imported weights file is a package.
            If True, and a new model was created, a package tag will be added.
        :param create_as_published: Boolean. If True, and a new model is created, it will be published.
        :param framework: optional, string name of the framework of the model or Framework
        """
        config_text = cls._resolve_config(config_text=config_text, config_dict=config_dict)
        weights_url = StorageHelper.conform_url(weights_url)
        result = _Model._get_default_session().send(models.GetAllRequest(
            uri=[weights_url],
            only_fields=["id", "name"],
            tags=["-" + ARCHIVED_TAG]
        ))

        if result.response.models:
            if not logger:
                logger = get_logger()

            logger.debug('A model with uri "{}" already exists. Selecting it'.format(weights_url))

            model = get_single_result(
                entity='model',
                query=weights_url,
                results=result.response.models,
                log=logger,
                raise_on_error=False,
            )

            logger.info("Selected model id: {}".format(model.id))

            return InputModel(model_id=model.id)

        base_model = _Model(
            upload_storage_uri=None,
            cache_dir=get_cache_dir(),
        )

        from .task import Task
        task = Task.current_task()
        if task:
            comment = 'Imported by task id: {}'.format(task.id) + ('\n'+comment if comment else '')
            project_id = task.project
            task_id = task.id
        else:
            project_id = None
            task_id = None

        if not framework:
            framework, file_ext = Framework._get_file_ext(
                framework=framework,
                filename=weights_url
            )

        base_model.update(
            design=config_text,
            labels=label_enumeration,
            name=name,
            comment=comment,
            tags=tags,
            uri=weights_url,
            framework=framework,
            project_id=project_id,
            task_id=task_id,
        )

        this_model = InputModel(model_id=base_model.id)
        this_model._base_model = base_model

        if is_package:
            this_model._set_package_tag()

        if create_as_published:
            this_model.publish()

        return this_model

    @classmethod
    def empty(
        cls,
        config_text=None,
        config_dict=None,
        label_enumeration=None,
    ):
        """
        Create an empty model, so that later we can execute the task in remote and
        replace the empty model with pre-trained model file

        :param config_text: model configuration (unconstrained text string). usually the content of a config_dict file.
            If `config_text` is not None, `config_dict` must not be provided.
        :param config_dict: model configuration parameters (dict).
            If `config_dict` is not None, `config_text` must not be provided.
        :param label_enumeration: dictionary of string to integer, enumerating the model output to labels
            example: {'background': 0 , 'person': 1}
        """
        design = cls._resolve_config(config_text=config_text, config_dict=config_dict)

        this_model = InputModel(model_id=cls._EMPTY_MODEL_ID)
        this_model._base_model = m = _Model(
            cache_dir=None,
            upload_storage_uri=None,
            model_id=cls._EMPTY_MODEL_ID,
        )
        m._data.design = _Model._wrap_design(design)
        m._data.labels = label_enumeration
        return this_model

    def __init__(self, model_id):
        """
        Load model based on id, returned object is read-only and can be connected to a task

        Notice, we can override the input model when running remotely

        :param model_id: id (string)
        """
        super(InputModel, self).__init__()
        self._base_model_id = model_id
        self._base_model = None

    @property
    def id(self):
        return self._base_model_id

    def connect(self, task):
        """
        Connect current model with a specific task, only supported for preexisting models,

        i.e. not supported on objects created with create_and_connect()
        When running in debug mode (i.e. locally), the task is updated with the model object
        (i.e. task input model is the load_model_id)
        When running remotely (i.e. from a daemon) the model is being updated from the task
        Notice! when running remotely the load_model_id is ignored and loaded from the task object
        regardless of the code

        :param task: Task object
        """
        self._set_task(task)

        if running_remotely() and task.input_model and task.is_main_task():
            self._base_model = task.input_model
            self._base_model_id = task.input_model.id
        else:
            # we should set the task input model to point to us
            model = self._get_base_model()
            # try to store the input model id, if it is not empty
            if model.id != self._EMPTY_MODEL_ID:
                task.set_input_model(model_id=model.id)
            # only copy the model design if the task has no design to begin with
            if not self._task.get_model_config_text():
                task.set_model_config(config_text=model.model_design)
            if not self._task.get_labels_enumeration():
                task.set_model_label_enumeration(model.data.labels)

        # If there was an output model connected, it may need to be updated by
        # the newly connected input model
        self.task._reconnect_output_model()

    def _get_base_model(self):
        if self._base_model:
            return self._base_model

        if not self._base_model_id:
            # this shouldn't actually happen
            raise Exception('Missing model ID, cannot create an empty model')
        self._base_model = _Model(
            upload_storage_uri=None,
            cache_dir=get_cache_dir(),
            model_id=self._base_model_id,
        )
        return self._base_model

    def _get_model_data(self):
        return self._get_base_model().data


class OutputModel(BaseModel):
    """
    Create an output model for a task to store the training results in.

    By definition the Model is always connected to a task, and is automatically registered as its output model.
    The common use case is reusing the model object, and overriding the weights every stored snapshot.
    A user can create multiple output models for a task, think a snapshot after a validation test has a new high-score.
    The Model will be read-write and if config/label-enumeration are None,
    their values will be initialized from the task input model.
    """

    @property
    def published(self):
        if not self.id:
            return False
        return self._get_base_model().locked

    @property
    def config_text(self):
        """
        returns a string representing the model configuration (from prototxt to ini file or python code to evaluate)

        :return: string
        """
        return _Model._unwrap_design(self._get_model_data().design)

    @config_text.setter
    def config_text(self, value):
        """
        Update the model configuration, store a blob of text for custom usage
        """
        self.update_design(config_text=value)

    @property
    def config_dict(self):
        """
        returns a configuration dictionary parsed from the config_text text,
        usually representing the model configuration (from prototxt to ini file or python code to evaluate)

        :return: Dictionary
        """
        return self._text_to_config_dict(self.config_text)

    @config_dict.setter
    def config_dict(self, value):
        """
        Update the model configuration: model configuration parameters (dict).
        """
        self.update_design(config_dict=value)

    @property
    def labels(self):
        """
        Return the labels enumerator {str(label): integer(id)} as saved in the model object

        :return: labels_dict, dictionary with labels (text) keys and values as integers
        """
        return self._get_model_data().labels

    @labels.setter
    def labels(self, value):
        """
        update the labels enumerator {str(label): integer(id)} as saved in the model object
        """
        self.update_labels(labels=value)

    @property
    def upload_storage_uri(self):
        return self._get_base_model().upload_storage_uri

    def __init__(
        self,
        task,
        config_text=None,
        config_dict=None,
        label_enumeration=None,
        name=None,
        tags=None,
        comment=None,
        framework=None,
    ):
        """
        Create a new model and immediately connect it to a task.

        We do not allow for Model creation without a task, so we always keep track on how we created the models
        In remote execution, Model parameters can be overridden by the Task (such as model configuration & label enumerator)

        :param task: Task object
        :type task: Task
        :param config_text: model configuration (unconstrained text string). usually the content of a config_dict file.
            If `config_text` is not None, `config_dict` must not be provided.
        :param config_dict: model configuration parameters (dict).
            If `config_dict` is not None, `config_text` must not be provided.
        :param label_enumeration: dictionary of string to integer, enumerating the model output to labels
            example: {'background': 0 , 'person': 1}
        :type label_enumeration: dict[str: int] or None
        :param name: optional, name for the newly created model
        :param tags: optional, list of strings as tags
        :param comment: optional, string description for the model
        :param framework: optional, string name of the framework of the model or Framework
        """
        super(OutputModel, self).__init__(task=task)

        config_text = self._resolve_config(config_text=config_text, config_dict=config_dict)

        self._model_local_filename = None
        self._base_model = None
        self._floating_data = create_dummy_model(
            design=_Model._wrap_design(config_text),
            labels=label_enumeration or task.get_labels_enumeration(),
            name=name,
            tags=tags,
            comment='Created by task id: {}'.format(task.id) + ('\n' + comment if comment else ''),
            framework=framework,
            upload_storage_uri=task.output_uri,
        )
        self.connect(task)

    def connect(self, task):
        """
        Connect current model with a specific task, only supported for preexisting models,

        i.e. not supported on objects created with create_and_connect()
        When running in debug mode (i.e. locally), the task is updated with the model object
        (i.e. task input model is the load_model_id)
        When running remotely (i.e. from a daemon) the model is being updated from the task
        Notice! when running remotely the load_model_id is ignored and loaded from the task object
        regardless of the code

        :param task: Task object
        """
        if self._task != task:
            raise ValueError('Can only connect preexisting model to task, but this is a fresh model')

        if running_remotely() and task.is_main_task():
            self._floating_data.design = _Model._wrap_design(self._task.get_model_config_text())
            self._floating_data.labels = self._task.get_labels_enumeration()
        elif self._floating_data is not None:
            # we copy configuration / labels if they exist, obviously someone wants them as the output base model
            if _Model._unwrap_design(self._floating_data.design):
                task.set_model_config(config_text=self._floating_data.design)
            else:
                self._floating_data.design = _Model._wrap_design(self._task.get_model_config_text())

            if self._floating_data.labels:
                task.set_model_label_enumeration(self._floating_data.labels)
            else:
                self._floating_data.labels = self._task.get_labels_enumeration()

        self.task._save_output_model(self)

    def set_upload_destination(self, uri):
        """
        Set the uri to upload all the model weight files to.

        Files are uploaded separately to the destination storage (e.g. s3,gc,file) and then
        a link to the uploaded model is stored in the model object
        Notice: credentials for the upload destination will be pooled from the
        global configuration file (i.e. ~/trains.conf)

        :param uri: upload destination (string). example: 's3://bucket/directory/' or 'file:///tmp/debug/'
        :return: True if destination scheme is supported (i.e. s3:// file:// gc:// etc...)
        """
        if not uri:
            return

        # Test if we can update the model.
        self._validate_update()

        # Create the storage helper
        storage = StorageHelper.get(uri)

        # Verify that we can upload to this destination
        try:
            uri = storage.verify_upload(folder_uri=uri)
        except Exception:
            raise ValueError("Could not set destination uri to: %s [Check write permissions]" % uri)

        # store default uri
        self._get_base_model().upload_storage_uri = uri

    def update_weights(self, weights_filename=None, upload_uri=None, target_filename=None,
                       auto_delete_file=True, register_uri=None, iteration=None, update_comment=True):
        """
        Update the model weights from a locally stored model filename.

        Uploading the model is a background process, the call returns immediately.

        :param weights_filename: locally stored filename to be uploaded as is
        :param upload_uri: destination uri for model weights upload (default: previously used uri)
        :param target_filename: the newly created filename in the destination uri location (default: weights_filename)
        :param auto_delete_file: delete temporary file after uploading
        :param register_uri: register an already uploaded weights file (uri must be valid)
        :param update_comment: if True, model comment will be updated with local weights file location (provenance)
        :return: uploaded uri
        """

        def delete_previous_weights_file(filename=weights_filename):
            try:
                if filename:
                    os.remove(filename)
            except OSError:
                self._log.debug('Failed removing temporary file %s' % filename)

        # test if we can update the model
        if self.id and self.published:
            raise ValueError('Model is published and cannot be changed')

        if (not weights_filename and not register_uri) or (weights_filename and register_uri):
            raise ValueError('Model update must have either local weights file to upload, '
                             'or pre-uploaded register_uri, never both')

        # only upload if we are connected to a task
        if not self._task:
            raise Exception('Missing a task for this model')

        if weights_filename is not None:
            # make sure we delete the previous file, if it exists
            if self._model_local_filename != weights_filename:
                delete_previous_weights_file(self._model_local_filename)
            # store temp filename for deletion next time, if needed
            if auto_delete_file:
                self._model_local_filename = weights_filename

        # make sure the created model is updated:
        model = self._get_force_base_model()
        if not model:
            raise ValueError('Failed creating internal output model')

        # select the correct file extension based on the framework, or update the framework based on the file extension
        framework, file_ext = Framework._get_file_ext(
            framework=self._get_model_data().framework,
            filename=weights_filename or register_uri
        )

        if weights_filename:
            target_filename = target_filename or Path(weights_filename).name
            if not target_filename.lower().endswith(file_ext):
                target_filename += file_ext

        # set target uri for upload (if specified)
        if upload_uri:
            self.set_upload_destination(upload_uri)

        # let us know the iteration number, we put it in the comment section for now.
        if update_comment:
            comment = self.comment or ''
            iteration_msg = 'snapshot {} stored'.format(weights_filename or register_uri)
            if not comment.startswith('\n'):
                comment = '\n' + comment
            comment = iteration_msg + comment
        else:
            comment = None

        # if we have no output destination, just register the local model file
        if weights_filename and not self.upload_storage_uri and not self._task.storage_uri:
            register_uri = weights_filename
            weights_filename = None
            auto_delete_file = False
            self._log.info('No output storage destination defined, registering local model %s' % register_uri)

        # start the upload
        if weights_filename:
            if not model.upload_storage_uri:
                self.set_upload_destination(self.upload_storage_uri or self._task.storage_uri)

            output_uri = model.update_and_upload(
                model_file=weights_filename,
                task_id=self._task.id,
                async_enable=True,
                target_filename=target_filename,
                framework=self.framework or framework,
                comment=comment,
                cb=delete_previous_weights_file if auto_delete_file else None,
                iteration=iteration or self._task.get_last_iteration(),
            )
        elif register_uri:
            register_uri = StorageHelper.conform_url(register_uri)
            output_uri = model.update(uri=register_uri, task_id=self._task.id, framework=framework, comment=comment)
        else:
            output_uri = None

        # make sure that if we are in dev move we report that we are training (not debugging)
        self._task._output_model_updated()

        return output_uri

    def update_weights_package(self, weights_filenames=None, weights_path=None, upload_uri=None,
                               target_filename=None, auto_delete_file=True, iteration=None):
        """
        Update the model weights from a locally stored model files (or directory containing multiple files).

        Uploading the model is a background process, the call returns immediately.

        :param weights_filenames: list of locally stored filenames (list of strings)
        :type weights_filenames: list
        :param weights_path: directory path to package (all the files in the directory will be uploaded)
        :type weights_path: str
        :param upload_uri: destination uri for model weights upload (default: previously used uri)
        :param target_filename: the newly created filename in the destination uri location (default: weights_filename)
        :param auto_delete_file: delete temporary file after uploading
        :return: uploaded uri for the weights package
        """
        # create list of files
        if (not weights_filenames and not weights_path) or (weights_filenames and weights_path):
            raise ValueError('Model update weights package should get either directory path to pack or a list of files')

        if not weights_filenames:
            weights_filenames = list(map(six.text_type, Path(weights_path).glob('*')))

        # create packed model from all the files
        fd, zip_file = mkstemp(prefix='model_package.', suffix='.zip')
        try:
            with zipfile.ZipFile(zip_file, 'w', allowZip64=True, compression=zipfile.ZIP_STORED) as zf:
                for filename in weights_filenames:
                    zf.write(filename, arcname=Path(filename).name)
        finally:
            os.close(fd)

        # now we can delete the files (or path if provided)
        if auto_delete_file:
            def safe_remove(path, is_dir=False):
                try:
                    (os.rmdir if is_dir else os.remove)(path)
                except OSError:
                    self._log.info('Failed removing temporary {}'.format(path))

            for filename in weights_filenames:
                safe_remove(filename)
            if weights_path:
                safe_remove(weights_path, is_dir=True)

        if target_filename and not target_filename.lower().endswith('.zip'):
            target_filename += '.zip'

        # and now we should upload the file, always delete the temporary zip file
        comment = self.comment or ''
        iteration_msg = 'snapshot {} stored'.format(str(weights_filenames))
        if not comment.startswith('\n'):
            comment = '\n' + comment
        comment = iteration_msg + comment
        self.comment = comment
        uploaded_uri = self.update_weights(weights_filename=zip_file, auto_delete_file=True, upload_uri=upload_uri,
                                           target_filename=target_filename or 'model_package.zip',
                                           iteration=iteration, update_comment=False)
        # set the model tag (by now we should have a model object) so we know we have packaged file
        self._set_package_tag()
        return uploaded_uri

    def update_design(self, config_text=None, config_dict=None):
        """
        Update the model configuration, basically store a blob of text for custom usage

        Notice: this is done in a lazily, only when updating weights we force the update of configuration in the backend

        :param config_text: model configuration (unconstrained text string). usually the content of a config_dict file.
            If `config_text` is not None, `config_dict` must not be provided.
        :param config_dict: model configuration parameters (dict).
            If `config_dict` is not None, `config_text` must not be provided.
        :return: True if update was successful
        """
        if not self._validate_update():
            return

        config_text = self._resolve_config(config_text=config_text, config_dict=config_dict)

        if self._task:
            self._task.set_model_config(config_text=config_text)

        if self.id:
            # update the model object (this will happen if we resumed a training task)
            result = self._get_force_base_model().update(design=config_text, task_id=self._task.id)
        else:
            self._floating_data.design = _Model._wrap_design(config_text)
            result = Waitable()

        # you can wait on this object
        return result

    def update_labels(self, labels):
        """
        Update the model label enumeration {str(label): integer(id)}

        :param labels: dictionary with labels (text) keys and values as integers
            example: {'background': 0 , 'person': 1}
        :return:
        """
        validate_dict(labels, key_types=six.string_types, value_types=six.integer_types, desc='label enumeration')

        if not self._validate_update():
            return

        if self._task:
            self._task.set_model_label_enumeration(labels)

        if self.id:
            # update the model object (this will happen if we resumed a training task)
            result = self._get_force_base_model().update(labels=labels, task_id=self._task.id)
        else:
            self._floating_data.labels = labels
            result = Waitable()

        # you can wait on this object
        return result

    @classmethod
    def wait_for_uploads(cls, timeout=None, max_num_uploads=None):
        """
        Wait for any pending/in-progress model uploads. If no uploads are pending or in-progress, returns immediately.

        :param timeout: If not None, a floating point number specifying a timeout in seconds after which this call will
            return.
        :param max_num_uploads: Max number of uploads to wait for.
        """
        _Model.wait_for_results(timeout=timeout, max_num_uploads=max_num_uploads)

    def _get_force_base_model(self):
        if self._base_model:
            return self._base_model

        # create a new model from the task
        self._base_model = self._task.create_output_model()
        # update the model from the task inputs
        labels = self._task.get_labels_enumeration()
        config_text = self._task.get_model_config_text()
        parent = self._task.output_model_id or self._task.input_model_id
        self._base_model.update(
            labels=labels,
            design=config_text,
            task_id=self._task.id,
            project_id=self._task.project,
            parent_id=parent,
            name=self._floating_data.name or self._task.name,
            comment=self._floating_data.comment,
            tags=self._floating_data.tags,
            framework=self._floating_data.framework,
            upload_storage_uri=self._floating_data.upload_storage_uri
        )

        # remove model floating change set, by now they should have matched the task.
        self._floating_data = None

        # now we have to update the creator task so it points to us
        self._base_model.update_for_task(task_id=self._task.id, override_model_id=self.id)

        return self._base_model

    def _get_base_model(self):
        if self._floating_data:
            return self._floating_data
        return self._get_force_base_model()

    def _get_model_data(self):
        if self._base_model:
            return self._base_model.data
        return self._floating_data

    def _validate_update(self):
        # test if we can update the model
        if self.id and self.published:
            raise ValueError('Model is published and cannot be changed')

        return True


class Waitable(object):
    def wait(self, *_, **__):
        return True
