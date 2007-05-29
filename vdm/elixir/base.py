from datetime import datetime

from elixir import *
import elixir
import sqlalchemy

class State(elixir.Entity):

    elixir.has_field('name', elixir.Unicode) 

class Revision(elixir.Entity):

    # should default to pending category
    # elixir.belongs_to('state', of_kind='State', default=3)
    elixir.belongs_to('state', of_kind='State')
    elixir.has_field('number', elixir.Integer)
    elixir.has_field('author', elixir.Unicode)
    elixir.has_field('log_message', elixir.Unicode)
    # for a transaction this time it started
    # for a revision time it was completed
    elixir.has_field('timestamp', elixir.DateTime, default=datetime.now)
    elixir.belongs_to('base_revision', of_kind='Revision')

    def __init__(self, *arg, **kwargs):
        super(Revision, self).__init__(*arg, **kwargs)
        self._state_pending = State.get_by(name='pending')
        if self.state is None: # only on creation ...
            self.state = self._state_pending
        # TODO: sort this out
        # seemingly default value is not working if we do not flush (not in the
        # session???)
        self.timestamp = datetime.now()
        self.model = None
        # automatically flush so that this now exists in the db
        elixir.objectstore.flush()

    def is_transaction(self):
        is_txn = self.state == self._state_pending
        return is_txn

    def set_model(self, model):
        self.model = model

    def commit(self):
        print "Committing: ", self.id
        if not self.is_transaction():
            raise Exception('This is not a transaction')
        # TODO: generate the revision number in some better way
        self.number = self.id
        self.state = State.get_by(name='active')
        self.timestamp = datetime.now()
        # flush commits everything into the db
        elixir.objectstore.flush()


class DomainModelBase(object):

    def __init__(self, revision, transaction):
        super(DomainModelBase, self).__init__()
        self.revision = revision
        self.transaction = transaction

    @classmethod
    def initialise_repository(self):
        """Stub method used to initialise default data in the repository.

        Only run once when repository is first created.
        """
        pass


class Repository(object):

    def __init__(self, model_class):
        self.model_class = model_class

    def create_tables(self):
        elixir.create_all()

    def drop_tables(self):
        elixir.objectstore.clear()
        elixir.drop_all()
    
    def rebuild(self):
        "Rebuild the domain model."
        self.drop_tables()
        self.create_tables()
        self.init()

    def init(self):
        "Initialise the domain model with default data."
        State(name='active')
        State(name='deleted')
        State(name='pending')
        elixir.objectstore.flush()
        # do not use a transaction but insert directly to avoid the bootstrap
        # problem
        base_rev = Revision(
                number=1,
                log_message='Initialising the Repository',
                author='system',
                state = State.get_by(name='active'),
                )
        self.model_class.initialise_repository()
        elixir.objectstore.flush()

    def youngest_revision(self):
        # TODO: write a test to check that we only get 'active' revisions not
        # those which are inactive or aborted ...
        revs = self.history()
        print len(revs)
        for rev in revs:
            model = self.model_class(rev)
            rev.set_model(model)
            return rev
        # no revisions
        return None
    
    def get_revision(self, id):
        revs = Revision.select_by(number=id)
        if len(revs) == 0:
            raise Exception('Error: no revisions with id: %s' % id)
        elif len(revs) > 1:
            raise Exception('Error: more than one revision with id: %s' % id)
        else:
            rev = revs[0]
            model = self.model_class(rev)
            rev.set_model(model)
            return rev
    
    def begin_transaction(self, revision=None):
        if revision is None:
            revision = self.youngest_revision()
        txn = Revision(base_revision=revision)
        model = self.model_class(revision, txn)
        txn.set_model(model)
        return txn
    
    def history(self):
        """Get the history of the repository.

        Revisions will not allow you to be at the model as that will not
        work correctly (see comments at top of module).

        @return: a list of ordered revisions with youngest first.
        """
        active = State.get_by(name='active') 
        revisions = Revision.query()
        revisions = revisions.filter_by(state=active)
        revisions = revisions.order_by(sqlalchemy.desc('number'))
        revisions = revisions.select()
        return revisions


class ObjectRevisionEntity(elixir.Entity):

    # to be defined in inheriting classes
    # base_object_name = ''
    
    elixir.belongs_to('state', of_kind='State')

    def __init__(self, *args, **kwargs):
        super(ObjectRevisionEntity, self).__init__(args, kwargs)
        self.state_id = 1

    def copy(self, transaction):
        newvals = {}
        for col in self._descriptor.fields:
            if not col.startswith('revision') and col != 'id':
                value = getattr(self, col)
                newvals[col] = value
        newvals['revision'] = transaction
        newrev = self.__class__(**newvals)
        return newrev


def get_attribute_names(sqlobj_class):
    # extra attributes added into Revision classes that should not be available
    # in main object
    excluded = [ 'revision', 'base' ]
    results = []
    for col in sqlobj_class.sqlmeta.columns.keys():
        if col.endswith('ID'):
            col = col[:-2]
        if col not in excluded:
            results.append(col)
    return results


class VersionedDomainObject(elixir.Entity):

    version_class = None
    
    def __init__(self, *args, **kwargs):
        super(VersionedDomainObject, self).__init__(*args, **kwargs)
        self._version_operations_ok = False

    def set_revision(self, revision, transaction):
        self.revision = revision
        self.transaction = transaction
        self.have_working_copy = False
        # use history instead of revisions because revisions causes conflict
        # with sqlobject
        self.history = []
        self._get_revisions()
        self._version_operations_ok = True
        self._setup_versioned_m2m()

    def _setup_versioned_m2m(self):
        for name, module_name, object_name, join_object_name in self.m2m:
            # do some meta trickery to get class from the name
            # __import__('A.B') returns A unless fromlist is *not* empty
            mod = __import__(module_name, None, None, 'blah')
            obj = mod.__dict__[join_object_name]
            self.__dict__[name] = KeyedRegister(
                    type=obj,
                    key_name='id',
                    revision=self.revision,
                    transaction=self.transaction,
                    keyed_value_name=self.__class__.__name__.lower(),
                    keyed_value=self,
                    other_key_name=object_name.lower()
                    )

    def _assert_version_operations_ok(self):
        if not self._version_operations_ok:
            msg = 'No Revision is set on this object so it is not possible' + \
                    ' to do operations involving versioning.'
            raise Exception(msg)

    def _get_revisions(self):
        # if not based off any revision (should only happen if this is first
        # ever revision in entire repository)
        if self.revision is None:
            return
        ourrev = self.revision.number
        
        # TODO: more efficient way
        select = list(
                self.version_class.select_by(base=self)
                )
        revdict = {}
        for objrev in select:
            revdict[objrev.revision.number] = objrev
        revnums = revdict.keys()
        revnums.sort()
        if len(revnums) == 0: # should only happen if package only just created
            return
        created_revision = revnums[0]
        if created_revision > ourrev: # then package does not exist yet
            return
        # Take all package revisions with revision number <= ourrev
        # TODO: make more efficient
        for tt in revnums:
            if tt <= ourrev:
                self.history.append(revdict[tt])

    def _current(self):
        return self.history[-1]

    def __getattr__(self, attrname):
        if attrname in self.versioned_attributes:
            self._assert_version_operations_ok()
            return getattr(self._current(), attrname)
        else:
            raise AttributeError()

    def __setattr__(self, attrname, attrval):
        if attrname != 'versioned_attributes' and attrname in self.versioned_attributes:
            self._assert_version_operations_ok()
            self._ensure_working_copy()
            current = self._current()
            # print 'Setting attribute before: ', attrname, attrval, current
            setattr(current, attrname, attrval)
            # print 'Setting attribute after: ', attrname, attrval, current
        else:
            super(VersionedDomainObject, self).__setattr__(attrname, attrval)

    def _ensure_working_copy(self):
        if not self.have_working_copy:
            wc = self._new_working_copy()
            self.have_working_copy = True
            self.history.append(wc)

    def _new_working_copy(self):
        # 2 options: either we are completely new or based off existing current
        if len(self.history) > 0:
            return self._current().copy(self.transaction)
        else:
            if self.transaction is None:
                raise Exception('Unable to set attributes outside of a transaction')
            rev = self.version_class(
                    base=self.sqlobj,
                    revision=self.transaction)
            return rev

    def exists(self):
        # is this right -- what happens if we did not have anything at revision
        # and have just created something as part of the current transaction
        # ...
        return len(self.history) > 0

    def delete(self):
        deleted = State.get_by(name='deleted')
        self.state = deleted
    
    
    def purge(self):
        select = self.version_class.select_by(base=self)
        for rev in select:
            self.version_class.delete(rev)
        # because we have overriden delete have to play smart
        super(VersionedDomainObject, self).delete(self)


## ------------------------------------------------------
## Registers

class Register(object):

    def __init__(self, type, key_name):
        self.type = type
        self.key_name = key_name

    def create(self, **kwargs):
        return self.type(**kwargs)
    
    def get(self, key):
        if self.key_name != 'id':
            colobj = getattr(self.type.c, self.key_name)
            query = self.type.query()
            obj = query.selectone_by(colobj==key)
        else:
            obj = self.type.get(key)
        return obj
    
    def delete(self, key):
        self.purge(key)

    def purge(self, key):
        obj = self.get(key)
        self.type.delete(obj)

    def list(self):
        return list(self.type.select())

    def __iter__(self):
        return self.list().__iter__()

    def __len__(self):
        return len(self.list())


class VersionedDomainObjectRegister(Register):

    def __init__(self, type, key_name, revision, transaction=None, **kwargs):
        super(VersionedDomainObjectRegister, self).__init__(type, key_name)
        self.revision = revision
        self.transaction = transaction

    def create(self, **kwargs):
        newargs = dict(kwargs)
        obj = self.type(**newargs)
        obj.set_revision(self.revision, self.transaction)
        obj._ensure_working_copy()
        for key, value in kwargs.items():
            setattr(obj, key, value)
        return obj
    
    def get(self, key):
        obj = super(VersionedDomainObjectRegister, self).get(key)
        obj.set_revision(self.revision, self.transaction)
        if obj.exists():
            return obj
        else:
            msg = 'No object identified by %s exists at revision %s' % (key,
                    self.revision)
            raise Exception(msg)

    def list(self, state='active'):
        all_objs_ever = self.type.select()
        results = []
        for obj in all_objs_ever:
            obj.set_revision(self.revision, self.transaction)
            if obj.exists() and obj.state.name == state:
                results.append(obj)
        return results

    def delete(self, key):
        obj = self.get(key)
        obj.delete()

    def purge(self, key):
        obj = self.get(key)
        obj.purge()

class KeyedRegister(VersionedDomainObjectRegister):
    """Provide a register keyed by a certain value.

    E.g. you have a package with tags and you want to do for a given instance
    of Package (pkg say):

    pkg.tags.get(tag)
    """

    def __init__(self, *args, **kwargs):
        kvname = kwargs['keyed_value_name']
        kv = kwargs['keyed_value']
        del kwargs['keyed_value_name']
        del kwargs['keyed_value']
        super(KeyedRegister, self).__init__(*args, **kwargs)
        self.keyed_value_name = kvname
        self.keyed_value = kv
        self.other_key_name = kwargs['other_key_name']

    def create(self, **kwargs):
        newargs = dict(kwargs)
        newargs[self.keyed_value_name] = self.keyed_value
        return super(KeyedRegister, self).create(**newargs)

    def get(self, key):
        # key is an object now (though could also be a name)
        # add ID as will be a foreign key
        objref1 = getattr(self.type.q, self.keyed_value_name + 'ID')
        objref2 = getattr(self.type.q, self.other_key_name + 'ID')
        sel = self.type.select(sqlobject.AND(
            objref1 == self.keyed_value.id, objref2 == key.id)
            )
        sel = list(sel)
        if len(sel) == 0:
            msg = '%s not in this register' % key
            raise Exception(msg)
        # should have only one item
        newkey = sel[0].id
        return super(KeyedRegister, self).get(newkey)

    def list(self, state='active'):
        # TODO: optimize by using select directly
        # really inefficient ...
        all = super(KeyedRegister, self).list(state)
        results = []
        for item in all:
            val = getattr(item, self.keyed_value_name)
            if val == self.keyed_value:
                results.append(item)
        return results


