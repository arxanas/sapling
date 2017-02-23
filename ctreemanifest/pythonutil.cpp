// pythonutil.cpp - utilities to glue C++ code to python
//
// Copyright 2016 Facebook, Inc.
//
// This software may be used and distributed according to the terms of the
// GNU General Public License version 2 or any later version.
//
// no-check-code

#include "pythonutil.h"

PythonObj::PythonObj() :
    obj(NULL) {
}

PythonObj::PythonObj(PyObject *obj) {
  if (!obj) {
    if (!PyErr_Occurred()) {
      PyErr_SetString(PyExc_RuntimeError,
          "attempted to construct null PythonObj");
    }
    throw pyexception();
  }
  this->obj = obj;
}

PythonObj::PythonObj(const PythonObj& other) {
  this->obj = other.obj;
  Py_XINCREF(this->obj);
}

PythonObj::~PythonObj() {
  Py_XDECREF(this->obj);
}

PythonObj& PythonObj::operator=(const PythonObj &other) {
  Py_XDECREF(this->obj);
  this->obj = other.obj;
  Py_XINCREF(this->obj);
  return *this;
}

PythonObj::operator PyObject* () const {
  return this->obj;
}

/**
 * Function used to obtain a return value that will persist beyond the life
 * of the PythonObj. This is useful for returning objects to Python C apis
 * and letting them manage the remaining lifetime of the object.
 */
PyObject *PythonObj::returnval() {
  Py_XINCREF(this->obj);
  return this->obj;
}

/**
 * Invokes getattr to retrieve the attribute from the python object.
 */
PythonObj PythonObj::getattr(const char *name) {
  return PyObject_GetAttrString(this->obj, name);
}

/**
 * Executes the current callable object if it's callable.
 */
PythonObj PythonObj::call(const PythonObj &args) {
  PyObject *result = PyEval_CallObject(this->obj, args);
  return PythonObj(result);
}

/**
 * Invokes the specified method on this instance.
 */
PythonObj PythonObj::callmethod(const char *name, const PythonObj &args) {
  PythonObj function = this->getattr(name);
  return PyObject_CallObject(function, args);
}

PythonStore::PythonStore(PythonObj store) :
  _get(store.getattr("get")),
  _storeObj(store) {
}

PythonStore::PythonStore(const PythonStore &store) :
  _get(store._get),
  _storeObj(store._storeObj) {
}

ConstantStringRef PythonStore::get(const Key &key) {
  PythonObj arglist = Py_BuildValue("s#s#",
      key.name.c_str(), (Py_ssize_t)key.name.size(),
      key.node, (Py_ssize_t)BIN_NODE_SIZE);

  PyObject *result = PyEval_CallObject(_get, arglist);

  if (!result) {
    if (PyErr_Occurred()) {
      throw pyexception();
    }

    PyErr_Format(PyExc_RuntimeError,
        "unable to find tree '%.*s:...'", (int) key.name.size(), key.name.c_str());
    throw pyexception();
  }

  PythonObj resultobj(result);

  char *path;
  Py_ssize_t pathlen;
  if (PyString_AsStringAndSize((PyObject*)result, &path, &pathlen)) {
    throw pyexception();
  }

  char *buffer = new char[pathlen];
  memcpy(buffer, path, pathlen);
  return ConstantStringRef(buffer, pathlen);
}
